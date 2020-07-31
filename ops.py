# Copyright (c) Liu Ziyi.
# Licensed under the MIT license.

import torch
import torch.nn as nn

import logging

logger = logging.getLogger('nni')


class DropPath(nn.Module):
    def __init__(self, p=0.):
        """
        Drop path with probability.

        Parameters
        ----------
        p : float
            Probability of an path to be zeroed.
        """
        super().__init__()
        self.p = p

    def forward(self, x):
        if self.training and self.p > 0.:
            keep_prob = 1. - self.p
            # per data point mask
            # mask = torch.zeros((x.size(0), 1, 1, 1),
            #                    device=x.device).bernoulli_(keep_prob)
            mask = torch.zeros((x.size(0), 1, 1),
                               device=x.device).bernoulli_(keep_prob)  # 只有3个维度.
            return x / keep_prob * mask

        return x


class PoolBN(nn.Module):
    """
    AvgPool or MaxPool with BN. `pool_type` must be `max` or `avg`.
    """

    def __init__(self, pool_type, C, kernel_size, stride, padding, affine=True):
        super().__init__()
        if pool_type.lower() == 'max':
            self.pool = nn.MaxPool1d(kernel_size, stride, padding)
        elif pool_type.lower() == 'avg':
            self.pool = nn.AvgPool1d(
                kernel_size, stride, padding, count_include_pad=False)
        else:
            raise ValueError()

        self.bn = nn.BatchNorm1d(C, affine=affine)

    def forward(self, x):
        out = self.pool(x)
        out = self.bn(out)
        return out


class StdConv(nn.Module):
    """
    Standard conv: ReLU - Conv - BN
    """

    def __init__(self, C_in, C_out, kernel_size, stride, padding, affine=True):
        super().__init__()
        self.net = nn.Sequential(
            nn.ReLU(),
            nn.Conv1d(C_in, C_out, kernel_size, stride, padding, bias=False),
            nn.BatchNorm1d(C_out, affine=affine)
        )

    def forward(self, x):
        return self.net(x)


class FacConv(nn.Module):
    """
    Factorized conv: ReLU - Conv(Kx1) - Conv(1xK) - BN
    """

    def __init__(self, C_in, C_out, kernel_length, stride, padding, affine=True):
        super().__init__()
        self.net = nn.Sequential(
            nn.ReLU(),
            nn.Conv1d(C_in, C_in, (kernel_length, 1),
                      stride, padding, bias=False),
            nn.Conv1d(C_in, C_out, (1, kernel_length),
                      stride, padding, bias=False),
            nn.BatchNorm1d(C_out, affine=affine)
        )

    def forward(self, x):
        return self.net(x)


class DilConv(nn.Module):
    """
    (Dilated) depthwise separable conv.
    ReLU - (Dilated) depthwise separable - Pointwise - BN.
    If dilation == 2, 3x3 conv => 5x5 receptive field, 5x5 conv => 9x9 receptive field.
    """

    def __init__(self, C_in, C_out, kernel_size, stride, padding, dilation, affine=True):
        super().__init__()
        # 基本的卷积单元, 第一次的卷积C_in=C_out?, groups=C_in.
        # 第二次的kernel_size=1是为了做reduction吗？
        # dilation的意义还没有理解.
        self.net = nn.Sequential(
            nn.ReLU(),
            nn.Conv1d(C_in, C_in, kernel_size, stride, padding, dilation=dilation, groups=C_in,
                      bias=False),
            nn.Conv1d(C_in, C_out, 1, stride=1, padding=0, bias=False),
            nn.BatchNorm1d(C_out, affine=affine)
        )

    def forward(self, x):
        # logger.info("input size of x: {}".format(x.shape))
        return self.net(x)


class SepConv(nn.Module):
    """
    Depthwise separable conv.
    DilConv(dilation=1) * 2.
    """

    def __init__(self, C_in, C_out, kernel_size, stride, padding, affine=True):
        super().__init__()
        self.net = nn.Sequential(
            # 为什么要用2次卷积，第2次卷积为什么要将stride固定为1.
            DilConv(C_in, C_out, kernel_size, stride,
                    padding, dilation=1, affine=affine),
            DilConv(C_in, C_out, kernel_size, 1,
                    padding, dilation=1, affine=affine)
        )

    def forward(self, x):
        return self.net(x)


class FactorizedReduce(nn.Module):
    """
    Reduce feature map size by factorized pointwise (stride=2).
    stride=2将特征图的尺寸降低一半.注意：reduction的kernel size一般都是1.
    @C_in:  输出特征图的通道数
    @C_out: 输出特征图的通道数
    """

    def __init__(self, C_in, C_out, affine=True):
        super().__init__()
        self.relu = nn.ReLU()
        self.conv1 = nn.Conv1d(C_in, C_out // 2, 1,
                               stride=2, padding=0, bias=False)
        self.conv2 = nn.Conv1d(C_in, C_out // 2, 1,
                               stride=2, padding=0, bias=False)
        self.bn = nn.BatchNorm1d(C_out, affine=True)

    def forward(self, x):
        x = self.relu(x)
        # 这里是模仿的原有的实现, 为什么conv2的输入是x[:, 1:, 1:].
        out = torch.cat([self.conv1(x), self.conv2(x[:, :, 1:])], dim=1)
        out = self.bn(out)
        return out
