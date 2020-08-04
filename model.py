from collections import OrderedDict

import torch
import torch.nn as nn

import ops
from nni.nas.pytorch import mutables
import os

import logging

logger = logging.getLogger(__name__)


class AuxiliaryHead(nn.Module):
    """ Auxiliary head in 2/3 place of network to let the gradient flow well """

    def __init__(self, input_size, C, n_classes):
        """ assuming input size 7x7 or 8x8 """
        assert input_size in [7, 8]
        super().__init__()
        self.net = nn.Sequential(
            nn.ReLU(inplace=True),
            nn.AvgPool1d(5, stride=input_size - 5, padding=0,
                         count_include_pad=False),  # 2x2 out
            nn.Conv1d(C, 128, kernel_size=1, bias=False),
            nn.BatchNorm1d(128),
            nn.ReLU(inplace=True),
            nn.Conv1d(128, 768, kernel_size=2, bias=False),  # 1x1 out
            nn.BatchNorm1d(768),
            nn.ReLU(inplace=True)
        )
        self.linear = nn.Linear(768, n_classes)

    def forward(self, x):
        out = self.net(x)
        out = out.view(out.size(0), -1)  # flatten
        logits = self.linear(out)
        return logits


class Node(nn.Module):
    def __init__(self, node_id, num_prev_nodes, channels, num_downsample_connect):
        super().__init__()
        # 可以看到ops也是一个nn.ModuleList()对象，猜测这个对象可以是支持列表内多个计算op的
        # 串联，比如：
        self.ops = nn.ModuleList()
        choice_keys = []
        # 绝对注意：这里每一个node
        for i in range(num_prev_nodes):
            stride = 2 if i < num_downsample_connect else 1
            choice_keys.append("{}_p{}".format(node_id, i))
            self.ops.append(
                mutables.LayerChoice(OrderedDict([
                    ("maxpool", ops.PoolBN('max', channels, 3, stride, 1, affine=False)),
                    ("avgpool", ops.PoolBN('avg', channels, 3, stride, 1, affine=False)),
                    ("skipconnect", nn.Identity() if stride == 1 else ops.FactorizedReduce(
                        channels, channels, affine=False)),
                    ("sepconv3x3", ops.SepConv(channels,
                                               channels, 3, stride, 1, affine=False)),
                    ("sepconv5x5", ops.SepConv(channels,
                                               channels, 5, stride, 2, affine=False)),
                    ("dilconv3x3", ops.DilConv(channels,
                                               channels, 3, stride, 2, 2, affine=False)),
                    ("dilconv5x5", ops.DilConv(channels,
                                               channels, 5, stride, 4, 2, affine=False))
                ]), key=choice_keys[-1]))
        self.drop_path = ops.DropPath()
        self.input_switch = mutables.InputChoice(
            choose_from=choice_keys, n_chosen=2, key="{}_switch".format(node_id))

    def forward(self, prev_nodes):
        assert len(self.ops) == len(prev_nodes)
        # 每一个op其实包含了7个候选的op，在op(node)内部会把其中7个候选op都遍历一遍。并且把
        # 它们7个的计算结果做一个加和reduction="sum"，这和我之前认为从7个op里面选择1个的看
        # 法不同.
        # out = [op(node) for op, node in zip(self.ops, prev_nodes)]
        out = []
        # ii = 0
        for op, node in zip(self.ops, prev_nodes):
            # logger.info("ii: {}".format(ii))
            # ii += 1
            # logger.info("input size of node: {}".format(node.shape))
            # if ii == 2:
            # logger.info("node.shape: {}".format(node.shape))
            # prev_op = op
            # prev_node = node
            op_node = op(node)
            out.append(op_node)
        out = [self.drop_path(o) if o is not None else None for o in out]
        # 这里和op(node)内部的计算也有点相似，也是把候选输入的tensor做了reduction="sum"的
        # 加和操作.
        return self.input_switch(out)


class Cell(nn.Module):

    def __init__(self, n_nodes, channels_pp, channels_p, channels, reduction_p, reduction):
        super().__init__()
        self.reduction = reduction
        self.n_nodes = n_nodes

        # If previous cell is reduction cell, current input size does not match with
        # output size of cell[k-2]. So the output[k-2] should be reduced by preprocessing.
        if reduction_p:
            self.preproc0 = ops.FactorizedReduce(
                channels_pp, channels, affine=False)
        else:
            self.preproc0 = ops.StdConv(
                channels_pp, channels, 1, 1, 0, affine=False)
        self.preproc1 = ops.StdConv(
            channels_p, channels, 1, 1, 0, affine=False)

        # generate dag
        self.mutable_ops = nn.ModuleList()
        for depth in range(2, self.n_nodes + 2):
            self.mutable_ops.append(Node("{}_n{}".format("reduce" if reduction else "normal", depth),
                                         depth, channels, 2 if reduction else 0))

    def forward(self, s0, s1):
        # s0, s1 are the outputs of previous previous cell and previous cell, respectively.
        # 从这里可以看出，在当前cell中首先对前面两个cell的输出进行处理，这就是两个nodes.因此，每个cell
        # 的最后一个node其实只是做汇聚feature map，而不做reduce操作.
        tensors = [self.preproc0(s0), self.preproc1(s1)]

        for node in self.mutable_ops:
            # for t in tensors:
            # logger.info("-")
            cur_tensor = node(tensors)
            tensors.append(cur_tensor)
        # 注意：这里可以看出，最后一个node的作用就是汇聚cell中所有nodes的输出
        output = torch.cat(tensors[2:], dim=1)
        return output


class CNN(nn.Module):

    def __init__(self, input_size, in_channels, channels, n_classes, n_layers, n_nodes=4,
                 stem_multiplier=3, auxiliary=False):
        super().__init__()
        self.in_channels = in_channels  # 输入通道数目，改3->1
        self.channels = channels  # stem中feature map的数目
        self.n_classes = n_classes  # 类别数
        self.n_layers = n_layers  # 8个cell
        self.aux_pos = 2 * n_layers // 3 if auxiliary else -1  # aux_pos作用不清楚

        c_cur = stem_multiplier * self.channels  # 为何将stem中feature map的数目扩大3倍
        self.stem = nn.Sequential(
            # nn.Conv2d(in_channels, c_cur, 3, 1, 1, bias=False),
            nn.Conv1d(in_channels, c_cur, 3, 1, 1, bias=False),
            # nn.BatchNorm2d(c_cur),s
            nn.BatchNorm1d(c_cur)
        )  # stem仅包含了一个卷积层和一个BN层，为什么要加stem?

        # for the first cell, stem is used for both s0 and s1
        # [!] channels_pp and channels_p is output channel size, but c_cur is input channel size.
        # 一般cell的第一个node为previousprevious的输出，第二个node为previous的输出，但考虑
        # 第一个cell之前没有cell，所以它的第一个和第二个node都连接的是stem的输出.
        channels_pp, channels_p, c_cur = c_cur, c_cur, channels

        self.cells = nn.ModuleList()
        reduction_p, reduction = False, False  # 决定一个cell在接受前两个cell的输出时，是否要做reduction？
        for i in range(n_layers):
            reduction_p, reduction = reduction, False
            # Reduce featuremap size and double channels in 1/3 and 2/3 layer.
            if i in [n_layers // 3, 2 * n_layers // 3]:
                c_cur *= 2
                reduction = True
            # 在每个cell中，默认采用了4个nodes,加上前2个cell的输出一共6个nodes。论文中提及
            # 使用了7个nodes，难道最后一个node的作用时决定是否对当前cell的输出做reduction.
            cell = Cell(n_nodes, channels_pp, channels_p,
                        c_cur, reduction_p, reduction)
            self.cells.append(cell)
            # 这里注意：一个cell的输出总通道数是所有的node的输出通道数的加和，但是有两个疑问：
            # 1. 不同node的输出feature map在尺寸上如何匹配；
            # 2. 为何只算了7个node中的3-6个，没有算上第7个
            # 目前可以看出，前面4个节点的输出最终都汇聚到了第7个节点上，作为统一的输出节点.而
            # 每一个节点的输出都是c_cur，所以总的输出节点数目是c_cur * n_nodes
            c_cur_out = c_cur * n_nodes
            channels_pp, channels_p = channels_p, c_cur_out

            if i == self.aux_pos:
                # self.aux_head = AuxiliaryHead(
                #     input_size // 4, channels_p, n_classes)
                self.aux_head = AuxiliaryHead(8, channels_p, n_classes)
        # 在最后一个cell的输出后，增加了一个池化层和一个线性分类器
        # self.gap = nn.AdaptiveAvgPool2d(1)
        self.gap = nn.AdaptiveAvgPool1d(1)
        self.linear = nn.Linear(channels_p, n_classes)

    def forward(self, x):
        s0 = s1 = self.stem(x)

        aux_logits = None
        for i, cell in enumerate(self.cells):
            # 每一个cell都会输出一个新的s1，并结合其上一个cell的输出s0一起
            # 作为下一个cell的输入
            s0, s1 = s1, cell(s0, s1)
            if i == self.aux_pos and self.training:
                aux_logits = self.aux_head(s1)

        out = self.gap(s1)
        out = out.view(out.size(0), -1)  # flatten
        logits = self.linear(out)

        if aux_logits is not None:
            return logits, aux_logits
        return logits

    def drop_path_prob(self, p):
        for module in self.modules():
            if isinstance(module, ops.DropPath):
                module.p = p
