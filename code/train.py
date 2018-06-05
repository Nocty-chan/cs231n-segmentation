import torch
import torch.optim as optim
import torch.nn as nn

import numpy as np
import os
import shutil
from utils import *
from tensorboardX import SummaryWriter


class Trainer():
    def __init__(self, generator, discriminator, train_loader, val_loader, \
            gan_reg=1.0, d_iters=5, g_iters=5, weight_clip=1e-2, grad_clip=1e-1, noise_scale=1e-2, \
            disc_lr=1e-5, gen_lr=1e-2,
            train_gan=False, experiment_dir='./', resume=False, load_iter=None):
        """
        Training class for a specified model
        Args:
            net: (model) model to train
            train_loader: (DataLoader) train data
            val_load: (DataLoader) validation data
            gan_reg: Hyperparameter for the GAN loss (\lambda in the paper)
            experiment_dir: path to directory that saves everything
            resume: load from last saved checkpoint ?
        """
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        print ("Using device %s" % self.device)
        self._gen = generator.to(self.device)
        self.train_gan = train_gan
        beta1 = 0.5
        if discriminator is not None and self.train_gan:
            print ("Training GAN")
            self._disc = discriminator.to(self.device)
            self._discoptimizer = optim.Adam(self._disc.parameters(), lr=disc_lr, betas=(beta1, 0.999)) # Discriminator optimizer (needs to be separate)
            self._BCEcriterion = nn.BCEWithLogitsLoss()
        else:
            print ("Runing network without GAN loss.")
            self._disc = None
            self._discoptimizer = None
            self._BCEcriterion = None

        self._train_loader = train_loader
        self._val_loader = val_loader

        self._MCEcriterion = nn.CrossEntropyLoss() # self._train_loader.dataset.weights.to(self.device)) # Criterion for segmentation loss

        self._genoptimizer = optim.Adam(self._gen.parameters(), lr=gen_lr, betas=(beta1, 0.999)) # Generator optimizer
        self.gan_reg = gan_reg
        self.d_iters = d_iters
        self.g_iters =g_iters
        self.start_iter = 0
        self.start_total_iters = 0
        self.start_epoch = 0
        self.best_mIOU = 0
        self.weight_clip = weight_clip
        self.grad_clip = grad_clip
        self.noise_scale = noise_scale
        self.experiment_dir = experiment_dir
        self.best_path = os.path.join(experiment_dir, 'best.pth.tar')
        if resume:
            self.load_model(load_iter)

    def _train_batch(self, mini_batch_data, mini_batch_labels, mini_batch_labels_flat, mode):
        """
        Performs one gradient step on a minibatch of data
        Args:
            mini_batch_data: (torch.Tensor) shape (N, C_in, H, W)
                where self._gen operates on (C_in, H, W) dimensional images
            mini_batch_labels: (torch.Tensor) shape (N, C_out, H, W)
                a batch of (H, W) binary masks for each of C_out classes
            mini_batch_labels_flat: (torch.Tensor) shape (N, H, W)
                a batch of (H, W) binary masks for each of C_out classes
            mode: discriminator or generator training
        Return:
            d_loss: (float) discriminator loss
            g_loss: (float) generator loss
            segmentation_loss: (float) segmentation loss
        """
        # mini_batch_data = mini_batch_data.to(self.device) # Input image (B, 3, H, W)
        mini_batch_data = mini_batch_data.to(self.device) # Input image (B, 3, H, W)

        mini_batch_labels = mini_batch_labels.to(self.device).type(dtype=torch.float32) # Ground truth mask (B, C, H, W)
        mini_batch_labels_flat = mini_batch_labels_flat.to(self.device) # Groun truth mask flattened (B, H, W)
        gen_out = self._gen(mini_batch_data) # Segmentation output from generator (B, C, H , W)
        converted_mask = nn.functional.tanh(gen_out).to(self.device)
        # false_labels = torch.zeros((mini_batch_data.size()[0], 1)).to(self.device)
        true_labels = torch.ones((mini_batch_data.size()[0], 1)).to(self.device)
        smooth_false_labels, smooth_true_labels = smooth_labels(mini_batch_data.size()[0], self.device)
        if mode == 'disc' and self._disc is not None and self.train_gan:
            d_loss = 0
            self._discoptimizer.zero_grad()
            scores_false = self._disc(mini_batch_data, converted_mask) # (B,)
            jittered_labels = mini_batch_labels + self.noise_scale * (torch.randn_like(mini_batch_labels) + 0.5)
            scores_true = self._disc(mini_batch_data, jittered_labels) # (B,)
            true_positive, true_negative = true_positive_and_negative(scores_true.detach().cpu(), scores_false.detach().cpu())
            # d_loss = torch.mean(scores_false) - torch.mean(scores_true)
            d_loss = self._BCEcriterion(scores_false, smooth_false_labels) + self._BCEcriterion(scores_true, smooth_true_labels)
            d_loss.backward()
            d_grad_norm = torch.nn.utils.clip_grad_norm_(self._disc.parameters(), self.grad_clip)
            self._discoptimizer.step()
            # W-GAN weight clipping
            # for p in self._disc.parameters():
            #     p.data.clamp_(-self.weight_clip, self.weight_clip)
            return d_loss, d_grad_norm, true_positive, true_negative
        if mode == 'gen':
            self._genoptimizer.zero_grad()
            g_loss = 0
            # GAN part
            if self._disc is not None and self.train_gan:
                scores_false = self._disc(mini_batch_data, converted_mask)
                g_loss = self._BCEcriterion(scores_false, true_labels)
                # g_loss = -torch.mean(scores_false)
            # Minimize segmentation loss
            segmentation_loss = self._MCEcriterion(gen_out, mini_batch_labels_flat)
            gen_loss = segmentation_loss + self.gan_reg * g_loss
            gen_loss.backward()
            g_grad_norm = torch.nn.utils.clip_grad_norm_(self._gen.parameters(), self.grad_clip)
            self._genoptimizer.step()
            return g_loss, segmentation_loss, g_grad_norm

    def which_to_train(self, iters):
        """
        Pattern: train generator for self.g_iters, then discriminator for self.d_iters
        Input:
            iters: (int) number of iterations so far
        """
        return 'gen' if (not self.train_gan) or (iters % (self.g_iters + self.d_iters)) < self.g_iters else 'disc'

    def train(self, num_epochs, print_every=100, eval_every=500):
        """
        Trains the model for a specified number of epochs
        Args:
            num_epochs: (int) number of epochs to train
            print_every: (int) number of minibatches to process before
                printing loss. default=100
        """
        writer = SummaryWriter(self.experiment_dir)

        total_iters = self.start_total_iters
        iter = self.start_iter
        batch_size = self._train_loader.batch_size
        num_samples = len(self._train_loader.dataset)
        epoch_len = int(num_samples / batch_size)
        d_loss=0
        g_loss=0
        segmentation_loss=0
        if total_iters is None:
            total_iters = iter + epoch_len * self.start_epoch
            print ("Total_iters starts at {}".format(total_iters))
        for epoch in range(self.start_epoch, num_epochs):
            print ("Starting epoch {}".format(epoch))
            for mini_batch_data, mini_batch_labels, mini_batch_labels_flat in self._train_loader:
                mode = self.which_to_train(total_iters)
                if mode == 'disc':
                    self._disc.train()
                    d_loss, d_grad_norm, true_positive, true_negative = self._train_batch(
                            mini_batch_data, mini_batch_labels, mini_batch_labels_flat, mode)
                    writer.add_scalar('Train/DiscriminatorLoss', d_loss, total_iters)
                    writer.add_scalar('Train/DiscriminatorTotalGradNorm', d_grad_norm, total_iters)
                    writer.add_scalar('Train/DiscriminatorTruePositive', true_positive, total_iters)
                    writer.add_scalar('Train/DiscriminatorTrueNegative', true_negative, total_iters)
                else:
                    self._gen.train()
                    g_loss, segmentation_loss, g_grad_norm = self._train_batch(
                            mini_batch_data, mini_batch_labels, mini_batch_labels_flat, mode)
                    writer.add_scalar('Train/GeneratorTotalGradNorm', g_grad_norm, total_iters)
                writer.add_scalar('Train/SegmentationLoss', segmentation_loss, total_iters)
                if self._disc is not None and self.train_gan:
                    writer.add_scalar('Train/GeneratorLoss', g_loss, total_iters)
                    writer.add_scalar('Train/GanLoss', d_loss + g_loss, total_iters)
                    writer.add_scalar('Train/TotalLoss', self.gan_reg * (d_loss + g_loss) + segmentation_loss, total_iters)
                if total_iters % print_every == 0:
                    if self._disc is None or not self.train_gan:
                        print ('Loss at iteration {}/{}: {}'.format(iter, epoch_len - 1, segmentation_loss))
                    else:
                        print("D_loss {}, G_loss {}, Seg loss {} at iteration {}/{}".format(d_loss, g_loss, segmentation_loss, iter, epoch_len - 1))
                        print("Overall loss at iteration {} / {}: {}".format(iter, epoch_len - 1, self.gan_reg * (d_loss + g_loss) + segmentation_loss))
                if eval_every > 0 and total_iters % eval_every == 0:
                    val_pixel_acc, val_mIOU, per_class_accuracy = self.evaluate(self._val_loader, total_iters, ignore_background=True)
                    if self.best_mIOU < val_mIOU:
                        self.best_mIOU = val_mIOU
                    self.save_model(iter, total_iters, epoch, self.best_mIOU, self.best_mIOU == val_mIOU)
                    writer.add_scalar('Val/PixelAcc', val_pixel_acc, total_iters)
                    writer.add_scalar('Val/MeanIOU', val_mIOU, total_iters)
                    writer.add_scalar('Val/PerClassAcc', per_class_accuracy, total_iters)

                    print("Validation Mean IOU at iteration {}/{}: {}".format(iter, epoch_len - 1, val_mIOU))
                iter += 1
                total_iters += 1
            iter = 0


    def save_model(self, iter, total_iters, epoch, mIOU, is_best):
        save_dict = {
            'epoch': epoch,
            'iter': iter + 1,
            'total_iters': total_iters + 1,
            'gen_dict': self._gen.state_dict(),
            'best_mIOU': mIOU,
            'gen_opt' : self._genoptimizer.state_dict()
        }
        if self._disc is not None:
            save_dict['disc_dict'] = self._disc.state_dict()
            save_dict['disc_opt'] = self._discoptimizer.state_dict()
            save_dict['gan_reg'] = self.gan_reg
        save_path = os.path.join(self.experiment_dir, str(total_iters) + '.pth.tar')
        torch.save(save_dict, save_path)
        print ("=> Saved checkpoint '{}'".format(save_path))
        if is_best:
            shutil.copyfile(save_path, self.best_path)
            print ("=> Saved best checkpoint '{}'".format(self.best_path))

    def load_model(self, load_iters):
        if load_iters is None:
            save_path = os.path.join(self.experiment_dir, 'best.pth.tar')
        else:
            save_path = os.path.join(self.experiment_dir, str(load_iters) + '.pth.tar')
        if os.path.isfile(save_path):
            print("=> loading checkpoint '{}'".format(save_path))
            checkpoint = torch.load(save_path)
            self.start_iter = checkpoint['iter']
            self.start_total_iters = checkpoint.get('total_iters', None)
            self.start_epoch = checkpoint['epoch']
            self.best_mIOU = checkpoint['best_mIOU']
            self._gen.load_state_dict(checkpoint['gen_dict'])
            self._genoptimizer.load_state_dict(checkpoint['gen_opt'])
            if self._disc is not None:
                if 'disc_dict' in checkpoint:
                  self._disc.load_state_dict(checkpoint['disc_dict'])
                  self._discoptimizer.load_state_dict(checkpoint['disc_opt'])
                  self.gan_reg = checkpoint['gan_reg']

            print("=> loaded checkpoint '{}' (iter {})".format(save_path, checkpoint['iter']))
        else:
            print("=> no checkpoint found at '{}'".format(save_path))


    '''
    Evaluation methods
    '''
    def evaluate(self, loader, curr_iter, ignore_background=True, num_batches=None, save_mask=True):
        num_iters = 0
        num_classes = loader.dataset.numClasses
        metrics = [calc_pixel_accuracy, calc_mean_IoU, per_class_pixel_acc]
        states = [None] * len(metrics)
        self._gen.eval()
        for data, labels, gt_visual in loader:
            data = data.to(self.device)
            labels = labels.float().to(self.device)
            preds = convert_to_mask(self._gen(data)).to(self.device) # B x C x H x W

            if save_mask:
                save_dir = os.path.join(self.experiment_dir, str(curr_iter))
                if not os.path.exists(save_dir):
                    os.makedirs(save_dir)

                for i in range(len(data)):
                    img = de_normalize(data[i].detach().cpu().numpy())
                    gt_mask = gt_visual[i].detach().cpu().numpy()
                    pred_mask = np.argmax(preds[i].detach().cpu().numpy(), axis=0)
                    display_image = np.transpose(img, (1, 2, 0))
                    save_to_file(pred_mask, display_image, gt_mask, i, save_dir)
                save_mask = False

            if ignore_background:
                labels = labels.narrow(1, 0, num_classes-1)
                preds = preds.narrow(1, 0, num_classes-1)
            for i, metric in enumerate(metrics):
                states[i] = metric(labels, preds, states[i])
            num_iters += 1
            if num_batches is not None and num_iters >= num_batches:
                break
        return (s['final'] for s in states)

    def get_confusion_matrix(self, loader):
        ''' Method to get confusion matrix,
        Assumes gt_visual is of size B x H x W
        mask_pred is of size B x C x H x W
        returns C x C numpy array '''
        self._gen.eval()
        numClasses = loader.dataset.numClasses
        confusion_mat = np.zeros((numClasses, numClasses))
        for data, mask_gt, gt_visual in loader:
            data = data.to(self.device)
            mask_pred = convert_to_mask(self._gen(data)).numpy()
            mask_pred = np.transpose(mask_pred, (1, 0, 2, 3)) # C x B x H x W
            pred_labels = np.argmax(mask_pred, axis=0).reshape((-1,))
            gt_labels = gt_visual.numpy().reshape((-1,))
            x = pred_labels + numClasses * gt_labels
            bincount_2d = np.bincount(x.astype(np.int32),
                                  minlength=numClasses ** 2)
            assert bincount_2d.size == numClasses ** 2
            conf = bincount_2d.reshape((numClasses, numClasses))
            return conf
