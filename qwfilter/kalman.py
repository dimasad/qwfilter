"""Kalman filtering / smoothing module.

TODO
----
* Add derivative of SVD square root.
* Vectorize the UT functions.
* Make docstrings for all constructors.
* Implement filter Hessian.

Improvement ideas
-----------------
* Allow gradients and Hessian to be calculated offline, saving processing time
  at the cost of memory.

"""


import abc
import collections
import re

import attrdict
import numpy as np
import numpy.ma as ma
import numpy.linalg
import scipy.linalg

from . import utils


class DTKalmanFilterBase(metaclass=abc.ABCMeta):
    """Discrete-time Kalman filter/smoother abstract base class."""
    
    def __init__(self, model, x, Px, **options):
        """Create a discrete-time Kalman filter.
        
        Parameters
        ----------
        model :
            The underlying system model.
        
        """
        self.model = model
        """The underlying system model."""
        
        self.x = np.asarray(x)
        """State vector mean."""
        
        self.Px = np.asarray(Px)
        """State vector covariance."""
        
        self.k = options.get('k', 0)
        """Time index."""
        
        self.L = options.get('L', 0.0)
        '''Measurement log-likelihood.'''
        
        nq = model.nq
        nx = model.nx
        
        self.dL_dq = options.get('dL_dq', np.zeros(nq))
        '''Measurement log-likelihood derivative.'''

        self.dx_dq = options.get('dx_dq', np.zeros((nq, nx))):
        '''State vector derivative.'''
        
        self.dPx_dq = options.get('dPx_dq', np.zeros((nq, nx, nx))):
        '''State vector covariance derivative.'''
    
    @abc.abstractmethod
    def predict(self):
        """Predict the state distribution at a next time sample."""
        raise NotImplementedError("Pure abstract method.")
    
    @abc.abstractmethod
    def correct(self, y):
        """Correct the state distribution, given the measurement vector."""
        raise NotImplementedError("Pure abstract method.")
    
    def filter(self, y):
        y = np.asanyarray(y)
        N = len(y)
        x = np.zeros((N,) + np.shape(self.x))
        Px = np.zeros((N,) + np.shape(self.x) + (self.model.nx,))
        
        for k in range(N):
            x[k], Px[k] = self.correct(y[k])
            if k < N - 1:
                self.predict()
        
        return x, Px
    
    def pem_merit(self, y):
        y = np.asanyarray(y)
        N = len(y)
        
        for k in range(N):
            self.correct(y[k])
            self.update_likelihood()
            if k < N - 1:
                self.predict()
        
        return self.L
    
    def pem_gradient(self, y):
        y = np.asanyarray(y)
        N = len(y)
        
        for k in range(N):
            self.correct(y[k])
            self.correction_diff()
            self.update_likelihood()
            self.likelihood_diff()
            if k < N - 1:
                self.predict()
                self.prediction_diff()
        
        return self.dL_dq


def cholesky_sqrt_diff2(S, d2Q, work):
    """Second derivatives of lower triangular Cholesky decomposition."""
    S = np.asarray(S)
    dQ = work['dQ']
    dS = work['dS']
        
    n = S.shape[-1]
    m = dQ.shape[0]
    (i, j, k) = work['i j k']
    (ix, jx, kx) = work['ix jx kx']
    dQ_tril = work['dQ_tril']
    A_tril = work['A_tril']
    A_tril_inv = work['A_tril_inv']
    
    dA = np.zeros((m, n, n, n, n))
    dA[:, ix, jx, ix, kx] = dS[:, kx, jx]
    dA[:, ix, jx, jx, kx] += dS[:, kx, ix]
    dA_tril = dA[:, i, j][..., i, j]
    dA_tril_inv = -np.einsum('ij,ajk,kl', A_tril_inv, dA_tril, A_tril_inv)

    d2Q_tril = d2Q[..., i, j]
    d2S_tril = np.einsum('aij,...j->a...i', dA_tril_inv, dQ_tril)
    d2S_tril += np.einsum('ij,...j->...i', A_tril_inv, d2Q_tril)
    d2S = np.zeros(d2Q_tril.shape[:-1] + (n, n))
    d2S[..., j, i] = d2S_tril
    return d2S


class UnscentedTransformBase(metaclass=abc.ABCMeta):
    """Unscented transform base class."""
    
    def __init__(self, ni, **options):
        """Unscented transform object constructor.
        
        Parameters
        ----------
        ni : int
            Number of inputs
        
        Options
        -------
        kappa :
            Weight of the center sigma point. Zero by default.
        
        """
        self.ni = ni
        """Number of inputs."""
        
        self.kappa = options.get('kappa', 0.0)
        """Weight parameter of the center sigma point."""
        assert self.ni + self.kappa != 0
        
        self.nsigma = 2 * ni + (self.kappa != 0)
        """Number of sigma points."""
        
        weights = np.repeat(0.5 / (ni + self.kappa), self.nsigma)
        if self.kappa != 0:
            weights[-1] = self.kappa / (ni + self.kappa)
        self.weights = weights
        """Transform weights."""
    
    @abc.abstractmethod
    def sqrt(self, Q):
        """Unscented transform square root method."""
        raise NotImplementedError("Pure abstract method.")
    
    def sigma_points(self, i, Pi):
        """Generate sigma-points and their deviations.
        
        The sigma points are the lines of the returned matrix.
        """
        ni = self.ni
        S = self.sqrt((ni + self.kappa) * Pi)
        idev = np.zeros((self.nsigma, ni))
        idev[:ni] = S
        idev[ni:(2 * ni)] = -S
        isigma = idev + i
        
        self.isigma = isigma
        self.idev = idev
        return isigma
    
    def sigma_points_diff(self, di_dq, dPi_dq):
        """Derivative of sigma-points."""
        ni = self.ni
        
        dS_dq = self.sqrt_diff((ni + self.kappa) * dPi_dq)
        didev_dq = np.zeros((self.nsigma,) + di_dq.shape)
        didev_dq[:ni] = np.rollaxis(dS_dq, -2)
        didev_dq[ni:(2 * ni)] = -didev_dq[:ni]
        disigma_dq = didev_dq + di_dq
        
        self.didev_dq = didev_dq
        self.disigma_dq = disigma_dq
        return disigma_dq
    
    def transform(self, i, Pi, f):
        isigma = self.sigma_points(i, Pi)
        weights = self.weights
        
        osigma = f(isigma)
        o = np.einsum('k,ki', weights, osigma)
        odev = osigma - o
        Po = np.einsum('ki,kj,k', odev, odev, weights)
        
        self.osigma = osigma
        self.odev = odev
        self.o = o
        self.Po = Po
        return (o, Po)
    
    def transform_diff(self, df_dq, df_di, di_dq, dPi_dq):
        weights = self.weights
        isigma = self.isigma
        
        disigma_dq = self.sigma_points_diff(di_dq, dPi_dq)
        Dosigma_Dq = np.einsum('ijk,ilj->ilk', df_di(isigma), disigma_dq)
        Dosigma_Dq += df_dq(isigma)
        
        do_dq = np.einsum('k,k...', weights, Dosigma_Dq)
        dodev_dq = Dosigma_Dq - do_dq
        dPo_dq = np.einsum('klj,ki,k->lij', dodev_dq, self.odev, weights)
        dPo_dq += np.swapaxes(dPo_dq, -1, -2)
        
        self.Dosigma_Dq = Dosigma_Dq
        self.dodev_dq = dodev_dq
        self.do_dq = do_dq
        self.dPo_dq = dPo_dq
        return (do_dq, dPo_dq)
    
    def crosscov(self):
        return np.einsum('ki,kj,k', self.idev, self.odev, self.weights)
    
    def crosscov_diff(self):
        dPio_dq = np.einsum('kli,kj,k->lij', 
                            self.didev_dq, self.odev, self.weights)
        dPio_dq += np.einsum('ki,klj,k->lij', 
                             self.idev, self.dodev_dq, self.weights)
        return dPio_dq


class SVDUnscentedTransform(UnscentedTransformBase):
    """Unscented transform using singular value decomposition."""
    
    def sqrt(self, Q):
        """Unscented transform square root method."""
        [U, s, VT] = scipy.linalg.svd(Q)
        self.S = np.transpose(U * np.sqrt(s))
        return self.S


class DifferentiableCholesky:
    
    gradient_data_initialized = 0

    def initialize_gradient_data(self, ni):
        if self.gradient_data_initialized == ni:
            return
        
        self.ni_range = np.arange(ni)
        self.ni_tril_indices = np.tril_indices(ni)
        self.gradient_data_initialized = ni
 
    def __call__(self, Q):
        """Perform the Cholesky decomposition."""
        self.S = scipy.linalg.cholesky(Q, lower=False)
        return self.S
    
    def diff(self, dQ_dq):
        """Derivatives of Cholesky decomposition.
        
        Parameters
        ----------
        dQ_dq : (nq, ni, ni) array_like
            The derivatives of `Q` with respect to some parameter vector.
             Must be symmetric with respect to the last two axes, i.e., 
            `dQ_dq[..., i, j] == dQ_dq[..., j, i]` for all `i, j` pairs.
        
        Returns
        -------
        dS_dq : (nq, ni, ni) array_like
            The derivative of the Cholesky decomposition of `Q` with respect
            to the parameter vector.
        
        """
        nq = len(dQ_dq)
        ni = len(self.S)
        self.initialize_gradient_data(ni)
        
        k = self.ni_range
        i, j = self.ni_tril_indices
        ix, jx, kx = np.ix_(i, j, k)
        
        A = np.zeros((ni, ni, ni, ni))
        A[ix, jx, ix, kx] = self.S[kx, jx]
        A[ix, jx, jx, kx] += self.S[kx, ix]
        A_tril = A[i, j][..., i, j]
        A_tril_inv = scipy.linalg.inv(A_tril)
        
        dQ_dq_tril = dQ_dq[..., i, j]
        dS_dq_tril = np.einsum('ab,...b->...a', A_tril_inv, dQ_dq_tril)
        dS_dq = np.zeros((nq, ni, ni))
        dS_dq[..., j, i] = dS_dq_tril
        return dS_dq


class CholeskyUnscentedTransform(UnscentedTransformBase):
    """Unscented transform using Cholesky decomposition."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cholesky = DifferentiableCholesky()
    
    def sqrt(self, Q):
        """Unscented transform square root method."""
        return self.cholesky(Q)
    
    def sqrt_diff(self, dQ_dq):
        """Derivatives of Unscented transform Cholesky decomposition."""
        return self.cholesky.diff(dQ_dq)


def choose_ut_transform_class(options):
    """Choose an unscented transform class from an options dict."""
    sqrt = options.get('sqrt', 'cholesky')
    if sqrt == 'cholesky':
        return CholeskyUnscentedTransform
    elif sqrt == 'svd':
        return SVDUnscentedTransform
    else:
        raise ValueError("Invalid value for `sqrt` option.")


class DTUnscentedPredictor(DTKalmanFilterBase):
    
    def __init__(self, model, x, Px, **options):
        # Initialize base
        super().__init__(model, x, Px, **options)
        
        # Get transform options
        ut_options = options.copy()
        ut_options.update(utils.extract_subkeys(options, 'pred_ut_'))
        
        # Create the transform object
        UTClass = choose_ut_transform_class(ut_options)
        self.__ut = UTClass(model.nx, **ut_options)
    
    def predict(self):
        """Predict the state distribution at the next time index."""
        def f_fun(x):
            return self.model.f(self.k, x)
        
        f, Pf = self.__ut.transform(self.x, self.Px, f_fun)
        Q = self.model.Q(self.k, self.x)
        
        self.prev_x = self.x
        self.prev_Px = self.Px
        self.k += 1
        self.x = f
        self.Px = Pf + Q
        return self.x, self.Px
    
    def prediction_diff(self):
        """Calculate the derivatives of the prediction."""
        k = self.k - 1 
        x = self.prev_x
        
        def df_dq_fun(x):
            return self.model.df_dq(k, x)
        def df_dx_fun(x):
            return self.model.df_dx(k, x)
        Df_Dq, DPf_Dq = self.__ut.transform_diff(
            df_dq_fun, df_dx_fun, self.dx_dq, self.dPx_dq
        )
        dQ_dq = self.model.dQ_dq(k, x)
        dQ_dx = self.model.dQ_dx(k, x)
        DQ_Dq = dQ_dq + np.einsum('ij,jkl', self.dx_dq, dQ_dx)
        
        self.prev_dx_dq = self.dx_dq
        self.prev_dPx_dq = self.dPx_dq
        self.dx_dq = Df_Dq
        self.dPx_dq = DPf_Dq + DQ_Dq
    
    def _calculate_prediction_hess(self, dQ_dq, dQ_dx, ut_work):
        k = self.k
        x = self.x
        dx_dq = self.dx_dq
        dPx_dq = self.dPx_dq
        d2x_dq2 = self.d2x_dq2
        d2Px_dq2 = self.d2Px_dq2
        
        d2Q_dq2 = self.model.d2Q_dq2(k, x)
        d2Q_dq_dx = self.model.d2Q_dq_dx(k, x)
        D2Q_Dq2 = d2Q_dq2 + np.einsum('...aijk,...bi', dQ_dq_dx, dx_dq)
        D2Q_Dq2 += np.einsum('...aij,...jkl', d2x_dq2, dQ_dx)
        D2Q_Dq2 += np.einsum('...ij,...bjkl,...ab', dx_dq, d2Q_dx2, dx_dq)
        D2Q_Dq2 += np.einsum('...ij,...ajkl', dx_dq, d2Q_dq_dx)

        def Df_Dq_fun(x, dx_dq):
            df_dq = self.model.df_dq(k, x)
            df_dx = self.model.df_dx(k, x)
            d2f_dq2 = self.model.d2f_dq2(k, x)
            d2f_dq_dx = self.model.d2f_dq_dx(k, x)
            d2f_dq2  = self.model.d2f_dq2(k, x)
            Df_Dq = d2f_dq2 + np.einsum('...akc,...bk', d2f_dq_dx, dx_dq)
            Df_Dq += np.einsum('...abi,...ij', d2x_dq2, df_dx)
            Df_Dq += np.einsum('...ai,...bij', dx_dq, d2f_dq_dx)
            Df_Dq += np.einsum('...ai,...ijk,...bj', dx_dq, d2f_dx2, dx_dq)
            return Df_Dq
        Df_Dq, DPf_Dq = self.__ut.transform_diff(Df_Dq_fun, dx_dq, dPx_dq, work)


class DTUnscentedCorrector(DTKalmanFilterBase):
    
    def __init__(self, model, x, Px, **options):
        # Initialize base
        super().__init__(model, x, Px, **options)
        
        # Get transform options
        ut_options = options.copy()
        ut_options.update(utils.extract_subkeys(options, 'corr_ut_'))
        
        # Create the transform object
        UTClass = choose_ut_transform_class(ut_options)
        self.__ut = UTClass(model.nx, **ut_options)
    
    def correct(self, y):
        """Correct the state distribution, given the measurement vector."""
        # Get the y-mask
        mask = ma.getmaskarray(y)
        self.active = active = ~mask
        if np.all(mask):
            return self.x, self.Px
        
        # Remove inactive outputs
        y = ma.compressed(y)
        R = self.model.R()[np.ix_(active, active)]
        def h_fun(x):
            return self.model.h(self.k, x)[..., active]
        
        # Perform unscented transform
        h, Ph = self.__ut.transform(self.x, self.Px, h_fun)
        Pxh = self.__ut.crosscov()
        
        # Factorize covariance
        self.__chol = DifferentiableCholesky()
        Py = Ph + R
        PyC = self.__chol(Py)
        PyCI = scipy.linalg.inv(PyC)
        PyI = np.einsum('ik,jk', PyCI, PyCI)
        
        # Perform correction
        e = y - h
        K = np.einsum('ik,kj', Pxh, PyI)
        x_corr = self.x + np.einsum('ij,j', K, e)
        Px_corr = self.Px - np.einsum('ik,jl,kl', K, K, Py)
        
        # Save and return the correction data
        self.prev_x = self.x
        self.prev_Px = self.Px
        self.e = e
        self.x = x_corr
        self.Px = Px_corr
        self.Pxh = Pxh
        self.Py = Py
        self.PyI = PyI
        self.PyC = PyC
        self.PyCI = PyCI
        self.K = K
        return x_corr, Px_corr

    def correction_diff(self):
        """Calculate the derivatives of the correction."""
        if not np.any(self.active):
            return
        
        # Get the model and transform derivatives
        def dh_dq_fun(x):
            return self.model.dh_dq(self.k, x)[..., self.active]
        def dh_dx_fun(x):
            return self.model.dh_dx(self.k, x)[..., self.active]
        Dh_Dq, DPh_Dq = self.__ut.transform_diff(
            self.corr_ut, dh_dq_fun, dh_dx_fun, self.dx_dq, self.dPx_dq
        )
        dPxh_dq = self.__ut.crosscov_diff(self.corr_ut)
        dR_dq = self.model.dR_dq()[(...,) + np.ix_(self.active, self.active)]

        # Calculate the correction derivatives
        de_dq = -Dh_Dq
        dPy_dq = DPh_Dq + dR_dq
        dPyI_dq = -np.einsum('ij,ajk,kl', self.PyI, dPy_dq, self.PyI)
        dK_dq = np.einsum('ik,akj', self.Pxh, dPyI_dq)
        dK_dq += np.einsum('aik,kj', dPxh_dq, self.PyI)

        self.de_dq = de_dq
        self.dPy_dq = dPy_dq
        self.dPyI_dq = dPyI_dq
        self.prev_dx_dq = self.dx_dq.copy()
        self.prev_dPx_dq = self.dPx_dq.copy()
        self.dx_dq += np.einsum('...aij,...j', dK_dq, self.e)
        self.dx_dq += np.einsum('...ij,...aj', self.K, de_dq)
        self.dPx_dq -= np.einsum('...aik,...jl,...lk', dK_dq, self.K, self.Py)
        self.dPx_dq -= np.einsum('...ik,...ajl,...lk', self.K, dK_dq, self.Py)
        self.dPx_dq -= np.einsum('...ik,...jl,...alk', self.K, self.K, dPy_dq)
    
    def update_likelihood(self):
        """Update measurement log-likelihood."""
        if not np.any(self.active):
            return
        
        self.PyCD = np.einsum('...kk->...k', self.PyC)
        self.L -= 0.5 * np.einsum('...i,...ij,...j', self.e, self.PyI, self.e) 
        self.L -= np.log(self.PyCD).sum(-1)
    
    def likelihood_diff(self):
        """Calculate measurement log-likelihood derivatives."""
        if not np.any(self.active):
            return
        
        # Get the work variables
        e = self.e
        PyI = self.PyI
        de_dq = self.de_dq
        dPyI_dq = self.dPyI_dq
        
        # Calculate the likelihood derivatives
        dPyC_dq = self.__chol.diff(self.dPy_dq)
        dPyCD_dq = np.einsum('...kk->...k', dPyC_dq)
        self.dL_dq -= np.sum(dPyCD_dq / self.PyCD, axis=-1)
        self.dL_dq -= 0.5 * np.einsum('...ai,...ij,...j', de_dq, PyI, e)
        self.dL_dq -= 0.5 * np.einsum('...i,...aij,...j', e, dPyI_dq, e)
        self.dL_dq -= 0.5 * np.einsum('...i,...ij,...aj', e, PyI, de_dq)


class DTUnscentedKalmanFilter(DTUnscentedPredictor, DTUnscentedCorrector):
    pass

