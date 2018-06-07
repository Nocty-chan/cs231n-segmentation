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
            gan_reg=1.0, weight_clip=1e-2, grad_clip=1e-1, noise_scale=1e-2, disc_lr=1e-5, gen_lr=1e-2, 
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
       
        self._gen = generator.cuda()
        self.train_gan = train_gan and discriminator is not None
        beta1 = 0.5
        if self.train_gan:
            print ("Training GAN")
            self._disc = discriminator.cuda()
            self._discoptimizer = optim.Adam(self._disc.parameters(), lr=disc_lr, betas=(beta1, 0.999)) # Discriminator optimizer (needs to be separate)
            self._BCEcriterion = nn.BCEWithLogitsLoss()
        else:
            self._disc = None
            print ("Runing network without GAN loss.")
            
        self._train_loader = train_loader
        self._val_loader = val_loader

        self._MCEcriterion = nn.CrossEntropyLoss() # self._train_loader.dataset.weights.cuda()) # Criterion for segmentation loss

        self._genoptimizer = optim.Adam(self._gen.parameters(), lr=gen_lr, betas=(beta1, 0.999)) # Generator optimizer
        self.gan_reg = gan_reg
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

    def _train_batch(self, mini_batch_data, mini_batch_labels, mini_batch_labels_flat):
        """
        Performs one gradient step on a minibatch of data
        Args:
            mini_batch_data: (torch.Tensor) shape (N, C_in, H, W)
                where self._gen operates on (C_in, H, W) dimensional images
            mini_batch_labels: (torch.Tensor) shape (N, C_out, H, W)
                a batch of (H, W) binary masks for each of C_out classes
            mini_batch_labels_flat: (torch.Tensor) shape (N, H, W)
                a batch of (H, W) binary masks for each of C_out classes
        Return:
            d_loss: (float) discriminator loss
            g_loss: (float) generator loss
            segmentation_loss: (float) segmentation loss
        """
        data = mini_batch_data.cuda() # Input image (B, 3, H, W)
        labels = mini_batch_labels.cuda().type(dtype=torch.float32) # Ground truth mask (B, C, H, W)
        labels_flat = mini_batch_labels_flat.cuda() # Ground truth mask flattened (B, H, W)
        self._gen.train()
        gen_out = self._gen(data) # Segmentation output from generator (B, C, H , W)              

        if not self.train_gan:
            self._genoptimizer.zero_grad()
            segmentation_loss = self._MCEcriterion(gen_out, labels_flat)
            segmentation_loss.backward()
            g_grad_norm = torch.nn.utils.clip_grad_norm_(self._gen.parameters(), self.grad_clip)
            self._genoptimizer.step()
            return segmentation_loss, g_grad_norm
        else:
            # First backprop through gen_loss = mce(gen(data), label)) + reg * bce(disc(g(data), data), 1)
            self._disc.train()
            self._genoptimizer.zero_grad()
            converted_mask = nn.functional.sigmoid(gen_out.detach())
            _, smooth_true_labels = smooth_labels(data.size()[0])
            false_scores = self._disc(data, converted_mask)
            segmentation_loss = self._MCEcriterion(gen_out, labels_flat)
            g_loss = self._BCEcriterion(false_scores, smooth_true_labels)
            gen_loss = segmentation_loss + self.gan_reg * g_loss
            gen_loss.backward()
            g_grad_norm = torch.nn.utils.clip_grad_norm_(self._gen.parameters(), self.grad_clip)
            self._genoptimizer.step()
            
            # now backprop through disc_loss = bce(disc(gen(data), label), 1) +  bce(disc(data, label), 0)
            jittered_gen_mask = converted_mask.detach() + self.noise_scale * torch.randn_like(converted_mask)            
            false_scores = self._disc(data, jittered_gen_mask)
            jittered_labels = labels + self.noise_scale * torch.randn_like(labels)
            true_scores = self._disc(data, jittered_labels) # (B,)
            # true_scores = self._disc(data, labels) # (B,)
            smooth_false_labels, smooth_true_labels = smooth_labels(data.size()[0])
            self._discoptimizer.zero_grad()      
            d_loss = self._BCEcriterion(false_scores, smooth_false_labels) + self._BCEcriterion(true_scores, smooth_true_labels)
            d_loss.backward()
            d_grad_norm = torch.nn.utils.clip_grad_norm_(self._disc.parameters(), self.grad_clip)
            self._discoptimizer.step()
            return segmentation_loss, g_loss, d_loss, g_grad_norm, d_grad_norm
    

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
                if self.train_gan:
                    segmentation_loss, g_loss, d_loss, g_grad_norm, d_grad_norm = self._train_batch(
                            mini_batch_data, mini_batch_labels, mini_batch_labels_flat)
                    writer.add_scalar('Train/DiscriminatorLoss', d_loss, total_iters)
                    writer.add_scalar('Train/DiscriminatorTotalGradNorm', d_grad_norm, total_iters)
                    writer.add_scalar('Train/GeneratorLoss', g_loss, total_iters)
                    writer.add_scalar('Train/GanLoss', d_loss + g_loss, total_iters)
                    writer.add_scalar('Train/TotalLoss', self.gan_reg * (d_loss + g_loss) + segmentation_loss, total_iters)
                else:
                    segmentation_loss, g_grad_norm = self._train_batch(
                            mini_batch_data, mini_batch_labels, mini_batch_labels_flat)
                writer.add_scalar('Train/GeneratorTotalGradNorm', g_grad_norm, total_iters)
                writer.add_scalar('Train/SegmentationLoss', segmentation_loss, total_iters)
                
                if total_iters % print_every == 0:
                    if self.train_gan:
                        print("D_loss {}, G_loss {}, Seg loss {} at iteration {}/{}".format(d_loss, g_loss, segmentation_loss, iter, epoch_len - 1))
                        print("Overall loss at iteration {} / {}: {}".format(iter, epoch_len - 1, self.gan_reg * (d_loss + g_loss) + segmentation_loss))
                    else:
                        print ('Loss at iteration {}/{}: {}'.format(iter, epoch_len - 1, segmentation_loss))

                if eval_every > 0 and total_iters % eval_every == 0:
                    if self.train_gan:
                        true_positive, true_negative = self.true_positive_and_negative_rates(self._val_loader)
                        writer.add_scalar('Val/DiscriminatorTruePositive', true_positive, total_iters)
                        writer.add_scalar('Val/DiscriminatorTrueNegative', true_negative, total_iters)

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
        save_path = os.path.join(self.experiment_dir, 'last.pth.tar')
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
    def evaluate(self, loader, curr_iter, ignore_background=True, num_batches=None):
        num_iters = 0
        num_classes = loader.dataset.numClasses
        metrics = [calc_pixel_accuracy, calc_mean_IoU, per_class_pixel_acc]
        states = [None] * len(metrics)
        self._gen.eval()
        for data, labels, gt_visual in loader:
            data = data.cuda()
            labels = labels.float().cuda()
            preds = convert_to_mask(self._gen(data)).cuda() # B x C x H x W
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
            data = data.cuda()
            mask_pred = convert_to_mask(self._gen(data)).numpy()
            mask_pred = np.transpose(mask_pred, (1, 0, 2, 3)) # C x B x H x W
            pred_labels = np.argmax(mask_pred, axis=0).reshape((-1,))
            gt_labels = gt_visual.numpy().reshape((-1,))
            x = pred_labels + numClasses * gt_labels
            bincount_2d = np.bincount(x.astype(np.int32),
                                  minlength=numClasses ** 2)
            assert bincount_2d.size == numClasses ** 2
            conf = bincount_2d.reshape((numClasses, numClasses))
            confusion_mat += conf
        return confusion_mat
    
    def get_second_matrix(self, loader):
        ''' Method to get matrix of second largest classes,
        Assumes gt_visual is of size B x H x W
        mask_pred is of size B x C x H x W
        returns C x C numpy array '''
        self._gen.eval()
        numClasses = loader.dataset.numClasses
        confusion_mat = np.zeros((numClasses, numClasses))
        for data, mask_gt, gt_visual in loader:
            data = data.cuda()
            mask_pred = convert_to_mask(self._gen(data)).numpy()
            mask_pred = np.transpose(mask_pred, (1, 0, 2, 3)) # C x B x H x W
            second_largest = np.argsort(mask_pred, axis=0)[1].reshape((-1,))
            gt_labels = gt_visual.numpy().reshape((-1,))
            x = second_largest + numClasses * gt_labels
            bincount_2d = np.bincount(x.astype(np.int32),
                                  minlength=numClasses ** 2)
            assert bincount_2d.size == numClasses ** 2
            conf = bincount_2d.reshape((numClasses, numClasses))
            confusion_mat += conf
        return confusion_mat
        
    def true_positive_and_negative_rates(self, loader):
        true_positive = 0.0
        true_negative = 0.0
        total = 0.0
        for data, mask_gt, gt_visual in loader:
            data = data.cuda()
            mask_gt = mask_gt.float().cuda() # Ground truth mask (B, C, H, W)
            self._gen.eval()
            self._disc.eval()
            gen_out = self._gen(data) # Segmentation output from generator (B, C, H , W)              
            converted_mask = nn.functional.sigmoid(gen_out)
            false_scores = self._disc(data, converted_mask).detach().cpu().numpy()
            true_scores = self._disc(data, mask_gt).detach().cpu().numpy() # (B,)
            
            true_positive += (np.where(true_scores > 0.5, 1, 0)).sum()
            true_negative += (np.where(false_scores > 0.5, 1, 0)).sum()
            total += data.size()[0]
        return true_positive / total, 1.0 - (true_negative / total)
