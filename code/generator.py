import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
from utils import *

class _DecoderBlock(nn.Module):
    """
    CNN block for the decoder.
    """
    def __init__(self, in_channels, out_channels, num_conv_layers):
        """
        Args:
            in_channels: (int) number of input channels to this block
            out_channels: (int) number of out_channels from this block
            num_conv_layers: (int) number of total Conv2d filters. Must be >= 2
        """
        assert num_conv_layers >= 2
        super().__init__()
        middle_channels = in_channels // 2
        layers = [
            nn.ConvTranspose2d(in_channels, in_channels, kernel_size=2, stride=2),
            nn.Conv2d(in_channels, middle_channels, kernel_size=3, padding=1),
            nn.ReLU()
        ]
        layers += [
                      nn.Conv2d(middle_channels, middle_channels, kernel_size=3, padding=1),
                      nn.ReLU(),
                  ] * (num_conv_layers - 2)
        layers += [
            nn.Conv2d(middle_channels, out_channels, kernel_size=3, padding=1),
            nn.ReLU(),
        ]
        self.decode = nn.Sequential(*layers)

    def forward(self, x):
        """
        Forward pass for the decoder block
        Args:
            x: (torch.Tensor) input tensor to be decoded by block
        Return:
            (torch.Tensor) result of decoding input tensor
        """
        return self.decode(x)


class VerySmallNet(nn.Module):
    def __init__(self, num_classes, pretrained=True):
        super().__init__()
        self.net = nn.Conv2d(3, num_classes, kernel_size=3, padding=1)

    def forward(self, x):
        return self.net(x)

class SegNetSmall(nn.Module):
    """
    Smaller implementation of SegNet based off of vgg-11
    """
    def __init__(self, num_classes, pretrained=True):
        """
        Args:
            num_classes: (int) number of output classes to be predicted
            pretrained: (bool) if True loads vgg-11 pretrained weights. default=True
        """
        super().__init__()
        vgg = models.vgg11(pretrained)

        features = list(vgg.features.children())
        self.enc1 = nn.Sequential(*features[:3])  # C_out = 64
        self.enc2 = nn.Sequential(*features[3:6])  # C_out = 128
        self.enc3 = nn.Sequential(*features[6:11])  # C_out = 256
        self.enc4 = nn.Sequential(*features[11:16])  # C_out = 512
        self.enc5 = nn.Sequential(*features[16:])  # C_out = 512
        for param in self.parameters():
            param.requires_grad = False

        self.dec5 = nn.Sequential(
            *([nn.ConvTranspose2d(512, 512, kernel_size=2, stride=2)] +
              [nn.Conv2d(512, 512, kernel_size=3, padding=1),
               nn.ReLU()] * 2)
        )
        self.dec4 = _DecoderBlock(1024, 256, 2)
        self.dec3 = _DecoderBlock(512, 128, 2)
        self.dec2 = _DecoderBlock(256, 64, 2)
        self.dec1 = _DecoderBlock(128, num_classes, 2)
        initialize_weights(self.dec5, self.dec4, self.dec3, self.dec2, self.dec1)

    def forward(self, x):
        enc1 = self.enc1(x)
        enc2 = self.enc2(enc1)
        enc3 = self.enc3(enc2)
        enc4 = self.enc4(enc3)
        enc5 = self.enc5(enc4)

        dec5 = self.dec5(enc5)
        dec4 = self.dec4(torch.cat([enc4, dec5], 1))
        dec3 = self.dec3(torch.cat([enc3, dec4], 1))
        dec2 = self.dec2(torch.cat([enc2, dec3], 1))
        dec1 = self.dec1(torch.cat([enc1, dec2], 1))
        return dec1


class SegNetSmaller(nn.Module):
    """
    Smaller implementation of SegNet based off of vgg-11
    """
    def __init__(self, num_classes, pretrained=True, freeze_pretrained=False):
        """
        Args:
            num_classes: (int) number of output classes to be predicted
            pretrained: (bool) if True loads vgg-11 pretrained weights. default=True
        """
        super().__init__()
        vgg = models.vgg11(pretrained)

        features = list(vgg.features.children())

        self.enc1 = nn.Sequential(*features[:3])  # C_out = 64
        self.enc2 = nn.Sequential(*features[3:6])  # C_out = 128
        self.enc3 = nn.Sequential(*features[6:11])  # C_out = 256

        if freeze_pretrained:
            for param in self.parameters():
                param.requires_grad = False

        self.dec3 = nn.Sequential(
            *([nn.ConvTranspose2d(256, 128, kernel_size=2, stride=2)] +
              [nn.Conv2d(128, 128, kernel_size=3, padding=1),
               nn.ReLU()] * 2)
        )
        self.dec2 = _DecoderBlock(256, 64, 2)
        self.dec1 = _DecoderBlock(128, num_classes, 2)
        initialize_weights(self.dec3, self.dec2, self.dec1)

    def forward(self, x):
        enc1 = self.enc1(x)
        enc2 = self.enc2(enc1)
        enc3 = self.enc3(enc2)
        dec3 = self.dec3(enc3)
        dec2 = self.dec2(torch.cat([enc2, dec3], 1))
        dec1 = self.dec1(torch.cat([enc1, dec2], 1))
        return dec1


class SegNet16(nn.Module):
    """
    PyTorch implementation of the SegNet architecture that uses 13 encoder-decoder layers.

    """
    def __init__(self, num_classes, pretrained=True, freeze_pretrained=False):
        """
        Args:
            num_classes: (int) number of output classes to be predicted
            pretrained: (bool) if True loads vgg-16 pretrained weights. default=True
        """
        super().__init__()
        vgg = models.vgg16_bn(pretrained)

        features = list(vgg.features.children())
        print(len(features))
        self.enc1 = nn.Sequential(*features[:7]), # Enc1 C_out 64
        self.enc2 = nn.Sequential(*features[7:14]), # Enc2 C_out 128
        self.enc3 = nn.Sequential(*features[14:27]), # Enc3 C_out 256
        self.enc4 = nn.Sequential(*features[27:40]), # Enc4 C_out 512
        self.enc5 = nn.Sequential(*features[40:53]) # Enc5 C_out 512

        if freeze_pretrained:
            for param in self.parameters():
                param.requires_grad = False

        self.dec5 = nn.Sequential(
            *([nn.ConvTranspose2d(512, 512, kernel_size=2, stride=2)] +
              [nn.Conv2d(512, 512, kernel_size=3, padding=1),
               nn.ReLU()] * 2)
        )
        self.dec4 = _DecoderBlock(1024, 256, 2) # Dec4
        self.dec3 = _DecoderBlock(512, 128, 2) # Dec3
        self.dec2 = _DecoderBlock(256, 64, 2)# Dec2
        self.dec1 = _DecoderBlock(128, num_classes, 2) #Dec 1

        initialize_weights(self.dec5, self.dec4, self.dec3, self.dec2, self.dec1)

    def forward(self, x):
        enc1 = self.enc1(x)
        enc2 = self.enc2(enc1)
        enc3 = self.enc3(enc2)
        enc4 = self.enc4(enc3)
        enc5 = self.enc5(enc4)
        print (enc1.size())
        print (enc2.size())
        print (enc3.size())
        print (enc4.size())
        print (enc5.size())
        dec5 = self.dec5(enc5)
        dec4 = self.dec4(torch.cat([enc4, dec5], 1))
        dec3 = self.dec3(torch.cat([enc3, dec4], 1))
        dec2 = self.dec2(torch.cat([enc2, dec3], 1))
        dec1 = self.dec1(torch.cat([enc1, dec2], 1))

        return dec_1
