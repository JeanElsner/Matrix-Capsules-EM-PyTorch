import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import math

class PrimaryCaps(nn.Module):
    r"""Creates a primary convolutional capsule layer
    that outputs a pose matrix and an activation.

    Note that for computation convenience, pose matrix
    are stored in first part while the activations are
    stored in the second part.

    Args:
        A: output of the normal conv layer
        B: number of types of capsules
        K: kernel size of convolution
        P: size of pose matrix is P*P
        stride: stride of convolution

    Shape:
        input:  (*, A, h, w)
        output: (*, h', w', B*(P*P+1))
        h', w' is computed the same way as convolution layer
        parameter size is: K*K*A*B*P*P + B*P*P
    """
    def __init__(self, A=32, B=32, K=1, P=4, stride=1):
        super(PrimaryCaps, self).__init__()
        self.pose = nn.Conv2d(in_channels=A, out_channels=B*P*P,
                            kernel_size=K, stride=stride, bias=True)
        self.a = nn.Conv2d(in_channels=A, out_channels=B,
                            kernel_size=K, stride=stride, bias=True)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        p = self.pose(x)
        a = self.a(x)
        a = self.sigmoid(a) 
        out = torch.cat([p, a], dim=1)
        out = out.permute(0, 2, 3, 1)
        return out


class ConvCaps(nn.Module):
    r"""Create a convolutional capsule layer
    that transfer capsule layer L to capsule layer L+1
    by EM routing.

    Args:
        B: input number of types of capsules
        C: output number on types of capsules
        K: kernel size of convolution
        P: size of pose matrix is P*P
        stride: stride of convolution
        iters: number of EM iterations
        coor_add: use scaled coordinate addition or not
        w_shared: share transformation matrix across w*h.

    Shape:
        input:  (*, h,  w, B*(P*P+1))
        output: (*, h', w', C*(P*P+1))
        h', w' is computed the same way as convolution layer
        parameter size is: K*K*B*C*P*P + B*P*P
    """
    # cost = (beta_u+log(((f(x)*(V_ij-mu)^2)/(g(x)))^(1/2)))*h(x)
    def __init__(self, B=32, C=32, K=3, P=4, stride=2, iters=3,
                 coor_add=False, w_shared=False, device='cuda', _lambda=[]):
        super(ConvCaps, self).__init__()
        self.B = B
        self.C = C
        self.K = K
        self.P = P
        self.psize = P*P
        self.stride = stride
        self.iters = iters
        self.coor_add = coor_add
        self.w_shared = w_shared
        # constant
        self.eps = 1e-8
        self._lambda = torch.tensor(_lambda).to(device)
        self.ln_2pi = math.log(2*math.pi)
        self.sqrt_2 = math.sqrt(2)
        self.beta_u = nn.Parameter(torch.ones(C))
        self.beta_a = nn.Parameter(torch.ones(C))
        # Sparsifying the transformation matrices
        randn = torch.randn(1, K*K*B, C, P, P)
        p = torch.full_like(randn, .5).bernoulli()
        self.weights = nn.Parameter(randn*p/(P*P))
        
        # operators
        self.sigmoid = nn.Sigmoid()
        self.softmax = nn.Softmax(dim=2)
        
        self.to(device)
        self.device = device

    def mmstep(self, a_i, R, V, _lambda, beta_a, beta_u):
        """
            \mu^h_j = \dfrac{\sum_i r_{ij} V^h_{ij}}{\sum_i r_{ij}}
            (\sigma^h_j)^2 = \dfrac{\sum_i r_{ij} (V^h_{ij} - mu^h_j)^2}{\sum_i r_{ij}}
            cost_h = (\beta_u + log \sigma^h_j) * \sum_i r_{ij}
            a_j = logistic(\lambda * (\beta_a - \sum_h cost_h))

            Input:
                a_i:      (b, B, 1)
                R:         (b, B, C)
                v:         (b, B, C, P*P)
            Local:
                cost_h:    (b, C, P*P)
                R_sum:     (b, C, 1)
            Output:
                a_j:     (b, C, 1)
                mu:        (b, 1, C, P*P)
                sigma_sq:  (b, 1, C, P*P)
        """
        b, B, C, psize = V.size()
        eps = 1e-8
        R_a = R * a_i
        #R = R / (R.sum(dim=2, keepdim=True) + eps)
        R_sum = R_a.sum(dim=1, keepdim=True)
        coeff = R_a / (R_sum + eps)
        coeff = coeff.view(b, B, C, 1)
        
        mu = torch.sum(coeff * V, dim=1, keepdim=True)
        sigma_sq = torch.sum(coeff * (V - mu)**2, dim=1, keepdim=True) + eps
        
        R_sum = R_sum.view(b, C, 1)
        sigma_sq = sigma_sq.view(b, C, psize)
        sigma = sigma_sq.sqrt()
        cost_h = (beta_u.view(C, 1) + torch.log(sigma)) * R_sum
        
        a_j = self.sigmoid(_lambda*(beta_a - cost_h.sum(dim=2)))
        sigma_sq = sigma_sq.view(b, 1, C, psize)
        
        return a_j, mu, sigma_sq

    def m_step(self, a_in, R, v, eps, b, B, C, psize, _lambda):
        """
            \mu^h_j = \dfrac{\sum_i r_{ij} V^h_{ij}}{\sum_i r_{ij}}
            (\sigma^h_j)^2 = \dfrac{\sum_i r_{ij} (V^h_{ij} - mu^h_j)^2}{\sum_i r_{ij}}
            cost_h = (\beta_u + log \sigma^h_j) * \sum_i r_{ij}
            a_j = logistic(\lambda * (\beta_a - \sum_h cost_h))

            Input:
                a_in:      (b, B, 1)
                R:         (b, B, C, 1)
                v:         (b, B, C, P*P)
            Local:
                cost_h:    (b, C, P*P)
                R_sum:     (b, C, 1)
            Output:
                a_out:     (b, C, 1)
                mu:        (b, 1, C, P*P)
                sigma_sq:  (b, 1, C, P*P)
        """
        R_a = R * a_in
        #R = R / (R.sum(dim=2, keepdim=True) + eps)
        R_sum = R_a.sum(dim=1, keepdim=True)
        coeff = R / (R_sum + eps)
        coeff = coeff.view(b, B, C, 1)

        mu = torch.sum(coeff * v, dim=1, keepdim=True)
        sigma_sq = torch.sum(coeff * (v - mu)**2, dim=1, keepdim=True) + eps

        R_sum = R_sum.view(b, C, 1)
        sigma_sq = sigma_sq.view(b, C, psize)
        cost_h = (self.beta_u.view(C, 1) + torch.log(sigma_sq.sqrt())) * R_sum

        a_out = self.sigmoid(_lambda*(self.beta_a - cost_h.sum(dim=2)))
        sigma_sq = sigma_sq.view(b, 1, C, psize)

        return a_out, mu, sigma_sq

    def e_step(self, mu, sigma_sq, a_out, v, eps, b, C):
        """
            ln_p_j = sum_h \dfrac{(\V^h_{ij} - \mu^h_j)^2}{2 \sigma^h_j}
                    - sum_h ln(\sigma^h_j) - 0.5*\sum_h ln(2*\pi)
            r = softmax(ln(a_j*p_j))
              = softmax(ln(a_j) + ln(p_j))

            Input:
                mu:        (b, 1, C, P*P)
                sigma:     (b, 1, C, P*P)
                a_out:     (b, C, 1)
                v:         (b, B, C, P*P)
            Local:
                ln_p_j_h:  (b, B, C, P*P)
                ln_ap:     (b, B, C, 1)
            Output:
                r:         (b, B, C, 1)
        """
        ln_p_j_h = (-(v - mu)**2 / (self.sqrt_2 * sigma_sq)) - (.5*torch.log(2*math.pi*sigma_sq))
        ln_ap = ln_p_j_h.sum(3) + torch.log(a_out.view(b, 1, C))
        r = self.softmax(ln_ap)
        return r

    def caps_em_routing(self, v, a_in, C, eps, r):
        """
            Input:
                v:         (b, B, C, P*P)
                a_in:      (b, C, 1)
            Output:
                mu:        (b, 1, C, P*P)
                a_out:     (b, C, 1)

            Note that some dimensions are merged
            for computation convenient, that is
            `b == batch_size*oh*ow`,
            `B == self.K*self.K*self.B`,
            `psize == self.P*self.P`
        """
        b, B, c, psize = v.shape
        assert c == C
        assert (b, B, 1) == a_in.shape
        
        R = (torch.ones(b, B, C)/C).to(self.device)
        for iter_ in range(self.iters):
            #torch.autograd.set_grad_enabled(iter_ == (self.iters - 1))
            #a_out, mu, sigma_sq = self.m_step(a_in, r, v, eps, b, B, C, psize, self._lambda*(iter_*.01+1))
            #a_out, mu, sigma_sq = self.m_step(a_in, R, v, eps, b, B, C, psize, self._lambda[0]+(self._lambda[1]-self._lambda[0])*r)
            a_out, mu, sigma_sq = self.mmstep(a_in, R, v, self._lambda[0]+(self._lambda[1]-self._lambda[0])*r, self.beta_a, self.beta_u)
            if iter_ < self.iters - 1:
                R = self.e_step(mu, sigma_sq, a_out, v, eps, b, C)

        return mu, a_out

    def add_patches(self, x, B, K, psize, stride):
        """
            Shape:
                Input:     (b, H, W, B*(P*P+1))
                Output:    (b, H', W', K, K, B*(P*P+1))
        """
        b, h, w, c = x.shape
        assert h == w
        assert c == B*(psize+1)
        oh = ow = int((h - K + 1) / stride)
        idxs = [[(h_idx + k_idx) \
                for h_idx in range(0, h - K + 1, stride)] \
                for k_idx in range(0, K)]
        x = x[:, idxs, :, :]
        x = x[:, :, :, idxs, :]
        x = x.permute(0, 1, 3, 2, 4, 5).contiguous()
        return x, oh, ow

    def transform_view(self, x, w, C, P, w_shared=False):
        """
            For conv_caps:
                Input:     (b*H*W, K*K*B, P*P)
                Output:    (b*H*W, K*K*B, C, P*P)
            For class_caps:
                Input:     (b, H*W*B, P*P)
                Output:    (b, H*W*B, C, P*P)
        """
        b, B, psize = x.shape
        assert psize == P*P

        x = x.view(b, B, 1, P, P)
        if w_shared:
            hw = int(B / w.size(1))
            w = w.repeat(1, hw, 1, 1, 1)

        w = w.repeat(b, 1, 1, 1, 1)
        x = x.repeat(1, 1, C, 1, 1)
        v = torch.matmul(x, w)
        v = v.view(b, B, C, P*P)
        return v

    def add_coord(self, v, b, h, w, B, C, psize):
        """
            Shape:
                Input:     (b, H*W*B, C, P*P)
                Output:    (b, H*W*B, C, P*P)
        """
        assert h == w
        v = v.view(b, h, w, B, C, psize)
        coor = 1. * torch.arange(h) / h
        coor_h = torch.zeros(1, h, 1, 1, 1, self.psize).to(self.device)
        coor_w = torch.zeros(1, 1, w, 1, 1, self.psize).to(self.device)
        coor_h[0, :, 0, 0, 0, 0] = coor
        coor_w[0, 0, :, 0, 0, 1] = coor
        v = v + coor_h + coor_w
        v = v.view(b, h*w*B, C, psize)
        return v

    def forward(self, x, r):
        b, h, w, c = x.shape
        if not self.w_shared:
            # add patches
            x, oh, ow = self.add_patches(x, self.B, self.K, self.psize, self.stride)

            # transform view
            p_in = x[:, :, :, :, :, :self.B*self.psize].contiguous()
            a_in = x[:, :, :, :, :, self.B*self.psize:].contiguous()
            p_in = p_in.view(b*oh*ow, self.K*self.K*self.B, self.psize)
            a_in = a_in.view(b*oh*ow, self.K*self.K*self.B, 1)
            v = self.transform_view(p_in, self.weights, self.C, self.P)

            # em_routing
            p_out, a_out = self.caps_em_routing(v, a_in, self.C, self.eps, r)
            p_out = p_out.view(b, oh, ow, self.C*self.psize)
            a_out = a_out.view(b, oh, ow, self.C)
            out = torch.cat([p_out, a_out], dim=3)
        else:
            assert c == self.B*(self.psize+1)
            assert 1 == self.K
            assert 1 == self.stride
            p_in = x[:, :, :, :self.B*self.psize].contiguous()
            p_in = p_in.view(b, h*w*self.B, self.psize)
            a_in = x[:, :, :, self.B*self.psize:].contiguous()
            a_in = a_in.view(b, h*w*self.B, 1)

            # transform view
            v = self.transform_view(p_in, self.weights, self.C, self.P, self.w_shared)

            # coor_add
            if self.coor_add:
                v = self.add_coord(v, b, h, w, self.B, self.C, self.psize)

            # em_routing
            _, out = self.caps_em_routing(v, a_in, self.C, self.eps, r)

        return out


class MatrixCapsules(nn.Module):
    """A network with one ReLU convolutional layer followed by
    a primary convolutional capsule layer and two more convolutional capsule layers.

    Suppose image shape is 28x28x1, the feature maps change as follows:
    1. ReLU Conv1
        (_, 1, 28, 28) -> 5x5 filters, 32 out channels, stride 2 with padding
        x -> (_, 32, 14, 14)
    2. PrimaryCaps
        (_, 32, 14, 14) -> 1x1 filter, 32 out capsules, stride 1, no padding
        x -> pose: (_, 14, 14, 32x4x4), activation: (_, 14, 14, 32)
    3. ConvCaps1
        (_, 14, 14, 32x(4x4+1)) -> 3x3 filters, 32 out capsules, stride 2, no padding
        x -> pose: (_, 6, 6, 32x4x4), activation: (_, 6, 6, 32)
    4. ConvCaps2
        (_, 6, 6, 32x(4x4+1)) -> 3x3 filters, 32 out capsules, stride 1, no padding
        x -> pose: (_, 4, 4, 32x4x4), activation: (_, 4, 4, 32)
    5. ClassCaps
        (_, 4, 4, 32x(4x4+1)) -> 1x1 conv, 10 out capsules
        x -> pose: (_, 10x4x4), activation: (_, 10)

        Note that ClassCaps only outputs activation for each class

    Args:
        A: output channels of normal conv
        B: output channels of primary caps
        C: output channels of 1st conv caps
        D: output channels of 2nd conv caps
        E: output channels of class caps (i.e. number of classes)
        K: kernel of conv caps
        P: size of square pose matrix
        iters: number of EM iterations
        ...
    """
    def __init__(self, A=32, B=32, C=32, D=32, E=10, K=3, P=4, iters=3, device='cuda', _lambda=[]):
        super(MatrixCapsules, self).__init__()
        self.A, self.B, self.C, self.D, self.E, self.P = A, B, C, D, E, P
        self.conv1 = nn.Conv2d(in_channels=1, out_channels=A,
                               kernel_size=5, stride=2, padding=2)
        self.relu1 = nn.ReLU(inplace=False)
        self.primary_caps = PrimaryCaps(A, B, 1, P, stride=1)
        
        self.conv_caps1 = ConvCaps(B, C, K, P, stride=2, iters=iters, device=device, _lambda=_lambda[0])
        self.conv_caps2 = ConvCaps(C, D, K, P, stride=1, iters=iters, device=device, _lambda=_lambda[1])
        self.class_caps = ConvCaps(D, E, 1, P, stride=1, iters=iters, device=device, _lambda=_lambda[2],
                                        coor_add=True, w_shared=True)
        
        self.batch_norm_input = nn.BatchNorm2d(num_features=A, affine=True)
        self.drop_out_input = nn.Dropout2d(p=.2)
        
        self.batch_norm_3d_1 = nn.BatchNorm3d(B, affine=True)
        self.batch_norm_2d_1 = nn.BatchNorm2d(B, affine=True)
        
        self.batch_norm_3d_2 = nn.BatchNorm3d(C, affine=True)
        self.batch_norm_2d_2 = nn.BatchNorm2d(C, affine=True)
        
        self.batch_norm_3d_3 = nn.BatchNorm3d(D, affine=True)
        self.batch_norm_2d_3 = nn.BatchNorm2d(D, affine=True)
        
        self.drop_out_3d_1 = nn.Dropout(p=.1)
        self.drop_out_2d_1 = nn.Dropout(p=.1)
        
        self.drop_out_3d_2 = nn.Dropout(p=.1)
        self.drop_out_2d_2 = nn.Dropout(p=.1)
        
        self.drop_out_3d_3 = nn.Dropout(p=.1)
        self.drop_out_2d_3 = nn.Dropout(p=.1)
        
        self.to(device)
        
    def forward(self, x, r):
        x = self.conv1(x)
        x = self.batch_norm_input(x)
        x = self.drop_out_input(x)
        x = self.relu1(x)
        x = self.primary_caps(x)
        #x = self.apply_batchnorm(x, self.B, self.batch_norm_2d_1, self.batch_norm_3d_1)
        x = self.apply_dropout(x, self.B, self.drop_out_2d_1, self.batch_norm_3d_1)
        x = self.conv_caps1(x, r)
        #x = self.apply_batchnorm(x, self.C, self.batch_norm_2d_2, self.batch_norm_3d_2)
        x = self.apply_dropout(x, self.C, self.drop_out_2d_2, self.batch_norm_3d_2)
        x = self.conv_caps2(x, r)
        #x = self.apply_batchnorm(x, self.D, self.batch_norm_2d_3, self.batch_norm_3d_3)
        x = self.apply_dropout(x, self.D, self.drop_out_2d_3, self.batch_norm_3d_3)
        x = self.class_caps(x, r)
        return x
    
    def apply_batchnorm(self, x, C, norm2d, norm3d):
        pose, a = x.split(C*self.P*self.P, 3)
        pose_view, a_view = pose.size(), a.size()
        pose = pose.view(pose_view[0:3] + (-1, 16))
        
        a = norm2d(a.permute(0, 3, 1, 2).contiguous()).permute(0, 2, 3, 1)
        pose = norm3d(pose.permute(0, 3, 1, 2, 4).contiguous()).permute(0, 2, 3, 1, 4)
        
        return torch.cat((pose.contiguous().view(pose_view), a.view(a_view)), dim=3)
    
    def apply_dropout(self, x, C, dropout2d, dropout3d):
        pose, a = x.split(C*self.P*self.P, 3)
        pose_view, a_view = pose.size(), a.size()
        pose = pose.view(pose_view[0:3] + (-1, 16))
        
        a = dropout2d(a.permute(0, 3, 1, 2).contiguous()).permute(0, 2, 3, 1)
        pose = dropout3d(pose.permute(0, 3, 1, 2, 4).contiguous()).permute(0, 2, 3, 1, 4)
        
        return torch.cat((pose.contiguous().view(pose_view), a.view(a_view)), dim=3)
    