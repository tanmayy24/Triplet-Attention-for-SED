import torch.nn as nn
import torch
import math
import torch.nn.functional as F
from .attention.TripletAttention import TripletAttention

class Hswish(nn.Module):
    def __init__(self, inplace=True):
        super(Hswish, self).__init__()
        self.inplace = inplace

    def forward(self, x):
        return x * F.relu6(x + 3., inplace=self.inplace) / 6.


class Identity(nn.Module):
    def __init__(self, channel):
        super(Identity, self).__init__()

    def forward(self, x):
        return x

class MobileBottleneck(nn.Module):
    def __init__(self, inp, oup, kernel, stride, attention=True, expansion= 4, nl='HS'):
        super(MobileBottleneck, self).__init__()
        exp = inp * expansion
        assert stride in [1, 2]
        assert kernel in [3, 5]
        padding = (kernel - 1) // 2
        self.use_res_connect = stride == 1 and inp == oup
        
        conv_layer = nn.Conv2d
        norm_layer = nn.BatchNorm2d
        if nl == 'RE':
            nlin_layer = nn.ReLU # or ReLU6
        elif nl == 'HS':
            nlin_layer = Hswish
        else:
            raise NotImplementedError
        if attention:
            AttentionLayer = TripletAttention
        else:
            AttentionLayer = Identity

        self.conv = nn.Sequential(
            # pw
            conv_layer(inp, exp, 1, 1, 0, bias=False),
            norm_layer(exp),
            nlin_layer(inplace=True),
            # dw
            conv_layer(exp, exp, kernel, stride, padding, groups=exp, bias=False),
            norm_layer(exp),
            nlin_layer(inplace=True),
            # attention-layer
            AttentionLayer(exp),
            # pw-linear
            conv_layer(exp, oup, 1, 1, 0, bias=False),
            norm_layer(oup),
        )

    def forward(self, x):
        if self.use_res_connect:
            return x + self.conv(x)
        else:
            return self.conv(x)

class GLU(nn.Module):
    def __init__(self, input_num):
        super(GLU, self).__init__()
        self.sigmoid = nn.Sigmoid()
        self.linear = nn.Linear(input_num, input_num)

    def forward(self, x):
        lin = self.linear(x.permute(0, 2, 3, 1))
        lin = lin.permute(0, 3, 1, 2)
        sig = self.sigmoid(x)
        res = lin * sig
        return res


class ContextGating(nn.Module):
    def __init__(self, input_num):
        super(ContextGating, self).__init__()
        self.sigmoid = nn.Sigmoid()
        self.linear = nn.Linear(input_num, input_num)

    def forward(self, x):
        lin = self.linear(x.permute(0, 2, 3, 1))
        lin = lin.permute(0, 3, 1, 2)
        sig = self.sigmoid(lin)
        res = x * sig
        return res

class BasicConv(nn.Module):
    def __init__(self, in_planes, out_planes, kernel_size, stride=1, padding=0, dilation=1, groups=1, relu=True, bn=True, bias=False):
        super(BasicConv, self).__init__()
        self.out_channels = out_planes
        self.conv = nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation, groups=groups, bias=bias)
        self.bn = nn.BatchNorm2d(out_planes, eps=1e-5, momentum=0.01, affine=True) if bn else None
        self.relu = nn.ReLU() if relu else None

    def forward(self, x):
        x = self.conv(x)
        if self.bn is not None:
            x = self.bn(x)
        if self.relu is not None:
            x = self.relu(x)
        return x

class Flatten(nn.Module):
    def forward(self, x):
        return x.view(x.size(0), -1)

class ChannelGate(nn.Module):
    def __init__(self, gate_channels, reduction_ratio=4, pool_types=['avg', 'max']):
        super(ChannelGate, self).__init__()
        self.gate_channels = gate_channels
        self.mlp = nn.Sequential(
            Flatten(),
            nn.Linear(gate_channels, gate_channels // reduction_ratio),
            nn.ReLU(),
            nn.Linear(gate_channels // reduction_ratio, gate_channels)
        )
        self.pool_types = pool_types

    def forward(self, x):
        channel_att_sum = None
        for pool_type in self.pool_types:
            if pool_type=='avg':
                avg_pool = F.avg_pool2d(x, (x.size(2), x.size(3)), stride=(x.size(2), x.size(3)))
                channel_att_raw = self.mlp(avg_pool)
            elif pool_type == 'max':
                max_pool = F.max_pool2d(x, (x.size(2), x.size(3)), stride=(x.size(2), x.size(3)))
                channel_att_raw = self.mlp(max_pool)
            elif pool_type == 'lp':
                lp_pool = F.lp_pool2d(x, 2, (x.size(2), x.size(3)), stride=(x.size(2), x.size(3)))
                channel_att_raw = self.mlp(lp_pool)
            elif pool_type == 'lse':
                # LSE pool only
                lse_pool = logsumexp_2d(x)
                channel_att_raw = self.mlp(lse_pool)

            if channel_att_sum is None:
                channel_att_sum = channel_att_raw
            else:
                channel_att_sum += channel_att_raw

        scale = F.sigmoid(channel_att_sum).unsqueeze(2).unsqueeze(3).expand_as(x)
        return x*scale


def logsumexp_2d(tensor):
    tensor_flatten = tensor.view(tensor.size(0), tensor.size(1), -1)
    s, _ = torch.max(tensor_flatten, dim=2, keepdim=True)
    outputs = s + (tensor_flatten-s).exp().sum(dim=2, keepdim=True).log()
    return outputs


class ChannelPool(nn.Module):
    def forward(self,x):
        return torch.cat((torch.max(x,1)[0].unsqueeze(1), torch.mean(x,1).unsqueeze(1)), dim=1)


class SpatialGate(nn.Module):
    def __init__(self):
        super(SpatialGate, self).__init__()
        kernel_size = 7
        self.compress = ChannelPool()
        self.spatial = BasicConv(2,1,kernel_size, stride=1, padding=(kernel_size-1)//2, relu=False)

    def forward(self, x):
        x_compress = self.compress(x)
        x_out = self.spatial(x_compress)
        scale = F.sigmoid(x_out)
        return x*scale


class CBAM(nn.Module):
    def __init__(self, gate_channels, reduction_ratio=4, pool_types=['avg', 'max'], no_spatial=False):
        super(CBAM, self).__init__()
        self.no_spatial = no_spatial
        self.ChannelGate = ChannelGate(gate_channels, reduction_ratio, pool_types)
        self.SpatialGate = SpatialGate() if not no_spatial else None

    def forward(self, x):
        x_out = self.ChannelGate(x)
        if not self.no_spatial:
            x_out = self.SpatialGate(x_out)
        return x_out


class ResidualCNN(nn.Module):
    def __init__(
        self,
        n_in_channel,
        activation="Relu",
        conv_dropout=0,
        kernel_size=[3, 3, 3],
        padding=[1, 1, 1],
        stride=[1, 1, 1],
        nb_filters=[64, 64, 64],
        pooling=[(1, 4), (1, 4), (1, 4)],
        normalization="batch",
        **transformer_kwargs
    ):
        """
            Initialization of CNN network s
        
        Args:
            n_in_channel: int, number of input channel
            activation: str, activation function
            conv_dropout: float, dropout
            kernel_size: kernel size
            padding: padding
            stride: list, stride
            nb_filters: number of filters
            pooling: list of tuples, time and frequency pooling
            normalization: choose between "batch" for BatchNormalization and "layer" for LayerNormalization.
        """
        super(ResidualCNN, self).__init__()

        self.nb_filters = nb_filters
        cnn = nn.Sequential()

        # stem block 0
        cnn.add_module('conv0', nn.Conv2d(n_in_channel, nb_filters[0], kernel_size=kernel_size[0], stride=stride[0], padding=padding[0]))
        cnn.add_module('batchnorm0', nn.BatchNorm2d(nb_filters[0], eps=0.001, momentum=0.99))
        cnn.add_module('glu0', GLU(nb_filters[0]))
        cnn.add_module('avgpool0', nn.AvgPool2d(pooling[0]))

        # stem block 1
        cnn.add_module('conv1', MobileBottleneck(nb_filters[0], nb_filters[1], kernel=kernel_size[1], stride=stride[1]))
        cnn.add_module('cbam1', CBAM(nb_filters[1]))
        cnn.add_module('avgpool1', nn.AvgPool2d(pooling[1]))
        
        # Residual block 0
        cnn.add_module('conv2', MobileBottleneck(nb_filters[1], nb_filters[2], kernel=kernel_size[2], stride=stride[2]))
        cnn.add_module('cbam2', CBAM(nb_filters[2]))
        cnn.add_module('avgpool2', nn.AvgPool2d(pooling[2]))
        
        # Residual block 1
        cnn.add_module('conv3', MobileBottleneck(nb_filters[2], nb_filters[3], kernel=kernel_size[3], stride=stride[3]))
        cnn.add_module('cbam3', CBAM(nb_filters[3]))
        cnn.add_module('avgpool3', nn.AvgPool2d(pooling[3]))

        # Residual block 2
        cnn.add_module('conv4', MobileBottleneck(nb_filters[3], nb_filters[4], kernel=kernel_size[4], stride=stride[4]))
        cnn.add_module('cbam4', CBAM(nb_filters[4]))
        cnn.add_module('avgpool4', nn.AvgPool2d(pooling[4]))

        # Residual block 3
        cnn.add_module('conv5', MobileBottleneck(nb_filters[4], nb_filters[5], kernel=kernel_size[5], stride=stride[5]))
        cnn.add_module('cbam5', CBAM(nb_filters[5]))
        cnn.add_module('avgpool5', nn.AvgPool2d(pooling[5]))

        # Residual block 4
        cnn.add_module('conv6', MobileBottleneck(nb_filters[5], nb_filters[6], kernel=kernel_size[6], stride=stride[6]))
        cnn.add_module('cbam6', CBAM(nb_filters[6]))
        cnn.add_module('avgpool6', nn.AvgPool2d(pooling[6]))
        # cnn
        self.cnn = cnn

    def forward(self, x):
        """
        Forward step of the CNN module

        Args:
            x (Tensor): input batch of size (batch_size, n_channels, n_frames, n_freq)

        Returns:
            Tensor: batch embedded
        """
        # conv features
        x = self.cnn(x)
        return x



