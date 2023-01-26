import torch
import torch.nn as nn
from mmcv.runner import load_checkpoint

from ...utils import cache_checkpoint
from ..builder import BACKBONES, build_model
from ..gcns.utils import unit_tcn


class MHSA(nn.Module):

    def __init__(self, dim, heads=8, dropout=0, reduction=2):
        super().__init__()
        self.dim = dim
        self.heads = heads
        self.dim_head = dim // reduction
        self.inner_dim = self.dim_head * heads
        self.dropout = nn.Dropout(dropout)

        self.scale = self.dim_head ** -0.5
        self.attend = nn.Softmax(dim=-1)

        self.to_qkv = nn.Linear(dim, self.inner_dim * 3, bias=False)
        self.to_out = nn.Linear(self.inner_dim, dim, bias=False)

    def forward(self, x):
        N, M, C = x.shape
        q, k, v = self.to_qkv(x).reshape((N, M, self.heads, 3 * self.dim_head)).chunk(3, dim=-1)

        dots = torch.einsum('nthc,nshc->nhts', q, k) * self.scale
        attn = self.attend(dots)
        attn = self.dropout(attn)

        out = torch.einsum('nhts,nshc->nthc', attn, v)
        out = torch.reshape(out, (N, M, -1))
        return self.to_out(out)


class TransformerEncoderLayer(nn.Module):

    def __init__(self,
                 dim,
                 heads=8,
                 reduction=2,
                 mlp_ratio=2,
                 dropout=0,
                 activation='gelu',
                 ln_type='postln',
                 ln_eps=1e-5,
                 **kwargs):
        super().__init__()
        self.self_attn = MHSA(dim, heads, dropout=dropout, reduction=reduction)

        dim_ffn = int(dim * mlp_ratio)
        self.fc1 = nn.Linear(dim, dim_ffn)
        self.dropout = nn.Dropout(dropout)
        self.fc2 = nn.Linear(dim_ffn, dim)

        assert ln_type in ['preln', 'postln']
        self.ln_type = ln_type

        self.attn_norm = nn.LayerNorm(dim, eps=ln_eps)
        self.ffn_norm = nn.LayerNorm(dim, eps=ln_eps)

        assert activation in ['relu', 'gelu']
        self.act = nn.ReLU() if activation == 'relu' else nn.GELU()

    def _sa_block(self, x):
        x = self.self_attn(x)
        return self.dropout(x)

    def _ff_block(self, x):
        x = self.fc2(self.dropout(self.act(self.fc1(x))))
        return self.dropout(x)

    def forward(self, x, **kwargs):
        if self.ln_type == 'preln':
            x = x + self._sa_block(self.attn_norm(x))
            x = x + self._ff_block(self.ffn_norm(x))
        else:
            x = self.attn_norm(x + self._sa_block(x))
            x = self.ffn_norm(x + self._ff_block(x))
        return x


class STInfoEmbed(nn.Module):

    def __init__(self, dim=128, mode='unified'):
        super().__init__()
        self.dim = dim
        self.mode = mode
        assert mode == 'unified'
        self.fc = nn.Linear(6, self.dim)

    def forward(self, x):
        assert torch.all(x >= 0) and torch.all(x <= 1)
        return self.fc(x)


class SkeleTR_neck(nn.Module):

    part_defn_dict = {
        'coco': [[0, 1, 2, 3, 4], [5, 7, 9], [6, 8, 10], [11, 13, 15], [12, 14, 16]],
        'openpose': [[0, 1, 14, 15, 16, 17], [2, 3, 4], [5, 6, 7], [8, 9, 10], [11, 12, 13]],
        'nturgb+d': [
            [2, 3], [0, 1, 4, 8, 12, 16, 20], [5, 6, 7, 21, 22], [9, 10, 11, 23, 24], [13, 14, 15], [17, 18, 19]
        ]
    }

    def _count_g(self, s, T=15, V=17):
        if len(s) != 2:
            return -1
        try:
            temporal = T if s[0] == 'T' else int(s[0])
            spatial = {'1': 1, 'P': 5, 'V': V}[s[1]]
            return spatial * temporal
        except:  # noqa: E722
            return -1

    def _legal_g(self, s):
        grans = s.split('+')
        for g in grans:
            if self._count_g(g) == -1:
                return False
        return True

    def partial_pool(self, x, gstr):
        # The shape of x should be (N, M, T, V, C)
        assert self._legal_g(gstr) and len(x.shape) == 5
        N, M, T, V, C = x.shape
        t_byte = gstr[0]
        if t_byte != 'T':
            t_byte = int(t_byte)
            x = nn.AdaptiveAvgPool3d((t_byte, ) + x.shape[-2:])(x)
        s_byte = gstr[1]
        if s_byte == '1':
            x = nn.AdaptiveAvgPool3d((x.shape[-3], 1, x.shape[-1]))(x)
        elif s_byte == 'P':
            parts = []
            for group in self.part_defn:
                parts.append(x[..., group, :].mean(dim=3, keepdims=True))
            x = torch.cat(parts, dim=3)
        return x.reshape(N, M, -1, 1, C)

    def mix_pooling(self, x, granularity):
        grans = granularity.split('+')
        results = []
        for g in grans:
            results.append(self.partial_pool(x, g))
        return torch.cat(results, dim=2)

    def __init__(self, layout, dim, granularity='11', embed_type=None, with_cls_token=False):
        super().__init__()
        self.layout = layout
        self.part_defn = self.part_defn_dict[layout]
        self.granularity = granularity
        self.embed_type = embed_type
        self.with_cls_token = with_cls_token
        self.dim = dim
        assert isinstance(granularity, str) and self._legal_g(granularity)
        if self.embed_type is not None:
            self.stinfo_embed = STInfoEmbed(dim)
            if 'ln' in self.embed_type:
                self.stinfo_ln = nn.LayerNorm(dim)

    def forward(self, x, stinfo=None):
        assert x.shape[-1] == self.dim
        x = self.mix_pooling(x, self.granularity)
        N, M, F, _, C = x.shape
        assert _ == 1
        x = x[..., 0, :]
        if stinfo is not None and hasattr(self, 'stinfo_embed'):
            assert stinfo.shape == (N, M, 6)
            stinfo_embed = self.stinfo_embed(stinfo)
            x = x + stinfo_embed[..., None, :]
            if hasattr(self, 'stinfo_ln'):
                x = self.stinfo_ln(x)
        x = x.reshape(N, -1, C)
        if self.with_cls_token:
            cls_token = torch.zeros_like(x[:, :1, :])
            return torch.cat([cls_token, x], dim=1)
        return x


@BACKBONES.register_module()
class SkeleTR(nn.Module):

    default_gcn_config = dict(type='STGCN', graph_cfg=dict(layout='coco', mode='spatial'))
    default_tr_config = dict(dim=None, num_tr=2, heads=8, reduction=2, mlp_ratio=2, ln_type='postln', dropout=0)
    default_neck_config = dict(granularity='11', embed_type=None, with_cls_token=False)

    def _merge_default_cfg(self):
        tups = [
            (self.gcn_config, self.default_gcn_config),
            (self.tr_config, self.default_tr_config),
            (self.neck_config, self.default_neck_config)
        ]
        for cfg, default_cfg in tups:
            for k, v in default_cfg.items():
                if k not in cfg:
                    cfg[k] = v

    def __init__(self,
                 gcn_config=dict(
                    type='STGCN',
                    gcn_adaptive='init',
                    gcn_with_res=True,
                    tcn_type='mstcn',
                    graph_cfg=dict(layout='coco', mode='spatial')),
                 tr_config=dict(
                    dim=None,
                    num_tr=2,
                    heads=8,
                    reduction=2,
                    mlp_ratio=2,
                    ln_type='postln',
                    dropout=0),
                 neck_config=dict(
                    granularity='11',
                    embed_type=None,
                    with_cls_token=False),
                 pretrained=None):
        super().__init__()

        self.gcn_config = gcn_config
        self.tr_config = tr_config
        self.neck_config = neck_config
        self.pretrained = pretrained
        self._merge_default_cfg()

        self.gcn = build_model(self.gcn_config)
        # Get the output dim of GCN
        device = self.gcn.data_bn.weight.device
        layout = self.gcn_config['graph_cfg']['layout']
        num_joint = {'coco': 17, 'openpose': 18, 'nturgb+d': 25}[layout]
        fake_input = torch.zeros(1, 1, 32, num_joint, 3).to(device)
        out_feat = self.gcn(fake_input)
        out_channels = out_feat.shape[2]

        dim = self.tr_config['dim'] if self.tr_config['dim'] is not None else out_channels
        self.tr_config['dim'] = dim
        self.trans_feat = None if dim == out_channels else unit_tcn(out_channels, dim, 1)

        self.neck = SkeleTR_neck(layout=layout, dim=dim, **self.neck_config)

        self.num_tr = self.tr_config.pop('num_tr')
        tr_layer = TransformerEncoderLayer(**self.tr_config)
        self.tr = nn.TransformerEncoder(tr_layer, num_layers=self.num_tr)

    def init_weights(self):
        if isinstance(self.pretrained, str):
            self.pretrained = cache_checkpoint(self.pretrained)
            load_checkpoint(self, self.pretrained, strict=False)

    def forward(self, x, stinfo=None):
        N, M, T, V, C = x.size()
        assert V in [17, 18, 25]

        if stinfo is not None:
            assert stinfo.shape == (N, M, 6)
            if stinfo.device != x.device:
                stinfo = stinfo.to(x.device)

        feat = self.gcn(x)
        if self.trans_feat is not None:
            feat = feat.reshape((N * M, ) + feat.shape[2:])
            feat = self.trans_feat(feat)
            feat = feat.reshape((N, M) + feat.shape[1:])

        x = feat.permute(0, 1, 3, 4, 2).contiguous()
        x = self.neck(x, stinfo=stinfo)
        x = self.tr(x)

        cls_token = None
        if self.neck.with_cls_token:
            cls_token = x[:, 0]
            x = x[:, 1:]
        x = x.reshape(N, M, -1, x.shape[-1]).mean(dim=2)

        return cls_token, x
