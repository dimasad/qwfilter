'''Kalman filtering / smoothing module.

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

'''


import abc
import collections
import re

import numpy as np
import numpy.ma as ma
import numpy.linalg
import scipy.linalg

from . import utils


class DTKalmanFilterBase(metaclass=abc.ABCMeta):
    '''Discrete-time Kalman filter/smoother abstract base class.'''
    
    def __init__(self, model, **options):
        '''Create a discrete-time Kalman filter.
        
        Parameters
        ----------
        model :
            The underlying system model.
        
        '''
        self.model = model
        '''The underlying system model.'''
    
    @abc.abstractmethod
    def predict(self, work):
        '''Predict the state distribution at a next time sample.'''
        raise NotImplementedError("Pure abstract method.")
    
    @abc.abstractmethod
    def correct(self, work, y):
        '''Correct the state distribution, given the measurement vector.'''
        raise NotImplementedError("Pure abstract method.")
    
    def filter(self, y):
        raise NotImplementedError("Does not yet represent API changes.")
        y = np.asanyarray(y)
        N = len(y)
        
        if self.save_history == 'filter':
            self.initialize_history(N)
        
        self.correct(y[0])
        for k in range(1, N):
            self.predict()
            self.correct(y[k])

    
def svd_sqrt(mat):
    '''SVD-based "square root" of a symmetric positive-semidefinite matrix.
    
    Used for unscented transform.
    
    Example
    -------
    Generate a random positive-semidefinite symmetric matrix.
    >>> np.random.seed(0)
    >>> A = np.random.randn(4, 10)
    >>> Q = np.dot(A, A.T)
    
    The square root should satisfy S'S = Q
    >>> S = svd_sqrt(Q)
    >>> STS = np.dot(S.T, S)
    >>> np.testing.assert_allclose(Q, STS)
    
    '''
    [U, s, VT] = numpy.linalg.svd(mat)
    return np.swapaxes(U * np.sqrt(s), -1, -2)



def ldl_sqrt(mat):
    '''LDL-based "square root" of a symmetric positive-semidefinite matrix.
    
    Used for unscented transform.
    
    Example
    -------
    Generate a random positive-semidefinite symmetric matrix.
    >>> np.random.seed(0)
    >>> A = np.random.randn(4, 10)
    >>> Q = np.dot(A, A.T)
    
    The square root should satisfy S'S = Q
    >>> S = ldl_sqrt(Q)
    >>> STS = np.dot(S.T, S)
    >>> np.testing.assert_allclose(Q, STS)
    
    '''
    L, D = numpy.linalg.ldl(mat)
    sqrt_D = np.sqrt(np.einsum('...ii->...i', D))    
    return np.einsum('...ij,...j->...ji', L, sqrt_D)


def cholesky_sqrt_diff(S, dQ=None, work=None):
    '''Derivatives of lower triangular Cholesky decomposition.
    
    Parameters
    ----------
    S : (n, n) array_like
        The upper triangular Cholesky decomposition of a matrix `Q`, i.e.,
        `S.T * S.T == Q`.
    dQ : (..., n, n) array_like or None
        The derivatives of `Q` with respect to some parameters. Must be
        symmetric with respect to the last two axes, i.e., 
        `dQ[...,i,j] == dQ[...,j,i]`. If `dQ` is `None` then the derivatives
        are taken with respect to `Q`, i.e., `dQ[i,j,i,j] = 1` and
        `dQ[i,j,j,i] = 1`.
    work : None or dict
        If not None, dictionary where the internal variables are saved for
        Hessian calculation.
    
    Returns
    -------
    dS : (..., n, n) array_like
        The derivative of `S` with respect to some parameters or with respect
        to `Q` if `dQ` is `None`.
    
    '''
    S = np.asarray(S)
    
    n = S.shape[-1]
    k = np.arange(n)
    i, j = np.tril_indices(n)
    ix, jx, kx = np.ix_(i, j, k)
    
    A = np.zeros((n, n, n, n))
    A[ix, jx, ix, kx] = S[kx, jx]
    A[ix, jx, jx, kx] += S[kx, ix]
    A_tril = A[i, j][..., i, j]
    A_tril_inv = scipy.linalg.inv(A_tril)
    
    if dQ is None:
        nnz = len(i)
        dQ_tril = np.zeros((n, n, nnz))
        dQ_tril[i, j, np.arange(nnz)] = 1
        dQ_tril[j, i, np.arange(nnz)] = 1
    else:
        dQ_tril = dQ[..., i, j]
    
    dS_tril = np.einsum('ab,...b->...a', A_tril_inv, dQ_tril)
    dS = np.zeros(dQ_tril.shape[:-1] + (n, n))
    dS[..., j, i] = dS_tril

    if work is not None:
        work['i j k'] = (i, j, k)
        work['ix jx kx'] = (ix, jx, kx)
        work['A_tril'] = A_tril
        work['A_tril_inv'] = A_tril_inv
        work['dQ_tril'] = dQ_tril
        work['dS'] = dS
        work['dQ'] = dQ
    
    return dS


def cholesky_sqrt_diff2(S, d2Q, work):
    '''Second derivatives of lower triangular Cholesky decomposition.'''
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
    
    def __init__(self, nin, **options):
        '''Unscented transform object constructor.
        
        Parameters
        ----------
        nin : int
            Number of inputs
        
        Options
        -------
        kappa :
            Weight of the center sigma point. Zero by default.
        
        '''
        self.nin = nin
        '''Number of inputs.'''
        
        self.kappa = options.get('kappa', 0.0)
        '''Relative weight of the center sigma point.'''
        assert self.nin + self.kappa != 0
        
        self.nsigma = 2 * nin + (self.kappa != 0)
        '''Number of sigma points.'''
        
        weights = np.repeat(0.5 / (nin + self.kappa), self.nsigma)
        if self.kappa != 0:
            weights[-1] = self.kappa / (nin + self.kappa)
        self.weights = weights
        '''Transform weights.'''
    
    class Work:
        '''Unscented transform work data.'''
        def __init__(self, xin, Pin):
            self.xin = xin
            self.Pin = Pin

    @abc.abstractmethod
    def sqrt(self, work, Q):
        '''Unscented transform square root method.'''
        raise NotImplementedError("Pure abstract method.")
    
    def gen_sigma_points(self, work):
        '''Generate sigma-points and their deviations.
        
        The sigma points are the lines of the returned matrix.
        '''
        xin = work.xin
        Pin = work.Pin
        kappa = self.kappa
        nin = self.nin
        
        Pin_sqrt = self.sqrt(work, (nin + kappa) * Pin)
        xin_dev = np.zeros((self.nsigma, nin))
        xin_dev[:nin] = Pin_sqrt
        xin_dev[nin:(2 * nin)] = -Pin_sqrt
        xin_sigma = xin_dev + xin
        
        work.xin_sigma = xin_sigma
        work.xin_dev = xin_dev
        return xin_sigma
    
    def transform(self, work, f):
        xin_sigma = self.gen_sigma_points(work)
        weights = self.weights
        
        xout_sigma = f(xin_sigma)
        xout = np.einsum('k,ki', weights, xout_sigma)
        xout_dev = xout_sigma - xout
        Pout = np.einsum('ki,kj,k', xout_dev, xout_dev, weights)
        
        work.xout_sigma = xout_sigma
        work.xout_dev = xout_dev
        work.xout = xout
        work.Pout = Pout
        return (xout, Pout)
    
    def crosscov(self, work):
        weights = self.weights
        xin_dev = work.xin_dev
        xout_dev = work.xout_dev
        return np.einsum('ki,kj,k', xin_dev, xout_dev, weights)
    
    def sigma_points_diff(self, mean_diff, cov_diff):
        '''Derivative of sigma-points.'''
        try:
            in_dev = self.in_dev
        except AttributeError:
            msg = "Transform must be done before requesting derivatives."
            raise RuntimeError(msg)
        
        nin = self.nin
        nq = len(mean_diff)
        kappa = self.kappa
        
        cov_sqrt = in_dev[:nin]
        cov_sqrt_diff = self.sqrt.diff(cov_sqrt, (nin + kappa) * cov_diff)
        in_dev_diff = np.zeros((self.nsigma,) + mean_diff.shape)
        in_dev_diff[:nin] = np.rollaxis(cov_sqrt_diff, -2)
        in_dev_diff[nin:(2 * nin)] = -in_dev_diff[:nin]
        in_sigma_diff = in_dev_diff + mean_diff
        self.in_dev_diff = in_dev_diff
        return in_sigma_diff
    
    def transform_diff(self, f_diff, mean_diff, cov_diff, crosscov=False):
        weights = self.weights
        try:
            in_dev = self.in_dev
            out_dev = self.out_dev
            in_sigma = self.in_sigma
        except AttributeError:
            msg = "Transform must be done before requesting derivatives."
            raise RuntimeError(msg)
        
        in_sigma_diff = self.sigma_points_diff(mean_diff, cov_diff)
        out_sigma_diff = f_diff(in_sigma, in_sigma_diff)
        out_mean_diff = np.einsum('k,k...', weights, out_sigma_diff)
        out_dev_diff = out_sigma_diff - out_mean_diff
        out_cov_diff = np.einsum('k...i,k...j,k->...ij',
                                 out_dev_diff, out_dev, weights)
        out_cov_diff += np.einsum('k...i,k...j,k->...ij',
                                  out_dev, out_dev_diff, weights)
        if crosscov:
            crosscov_diff = np.einsum('k...qi,k...j,k->...qij',
                                      self.in_dev_diff, out_dev, weights)
            crosscov_diff += np.einsum('k...i,k...qj,k->...qij',
                                       in_dev, out_dev_diff, weights)
            return (out_mean_diff, out_cov_diff, crosscov_diff)
        else:
            return (out_mean_diff, out_cov_diff)


class CholeskyUnscentedTransform(UnscentedTransformBase):
    '''Unscented transform using Cholesky decomposition.'''

    def sqrt(self, work, Q):
        '''Unscented transform square root method.'''
        return scipy.linalg.cholesky(Q, lower=False)


class SVDUnscentedTransform(UnscentedTransformBase):
    '''Unscented transform using singular value decomposition.'''
    
    def sqrt(self, work, Q):
        '''Unscented transform square root method.'''
        [U, s, VT] = scipy.linalg.svd(Q)
        return np.transpose(U * np.sqrt(s))


class DTUnscentedPredictor(DTKalmanFilterBase):
    
    def __init__(self, model, **options):
        # Initialize base
        super().__init__(model, **options)
        
        # Get transform options
        ut_options = options.copy()
        ut_options.update(utils.extract_subkeys(options, 'pred_ut_'))
        
        # Select transform class
        sqrt = ut_options.get('sqrt', 'cholesky')
        if sqrt == 'cholesky':
            UTClass = CholeskyUnscentedTransform
        elif sqrt == 'svd':
            UTClass = SVDUnscentedTransform
        else:
            raise ValueError("Invalid value for `sqrt` option.")
        
        # Create the transform object
        self.__ut = UTClass(model.nx, **ut_options)
    
    def predict(self, work):
        '''Predict the state distribution at the next time index.'''
        def f_fun(x):
            return self.model.f(work.k, x)
        
        work.pred_ut = self.__ut.Work(work.x, work.Px)
        f, Pf = self.__ut.transform(work.pred_ut, f_fun)
        Q = self.model.Q(k, work.x)
    
        work.prev_x = work.x
        work.prev_Px = work.Px
        work.k += 1
        work.x = f
        work.Px = Pf + Q
    
    def _calculate_prediction_grad(self):
        k = self.k
        x = self.x
        dx_dq = self.dx_dq
        dPx_dq = self.dPx_dq
        
        dQ_dq = self.model.dQ_dq(k, x)
        dQ_dx = self.model.dQ_dx(k, x)
        DQ_Dq = dQ_dq + np.einsum('...ij,...jkl', dx_dq, dQ_dx)
        
        def Df_Dq_fun(x, dx_dq):
            df_dq = self.model.df_dq(k, x)
            df_dx = self.model.df_dx(k, x)
            return df_dq + np.einsum('...qx,...xf->...qf', dx_dq, df_dx)
        work = {}
        Df_Dq, DPf_Dq = self.__ut.transform_diff(Df_Dq_fun, dx_dq, dPx_dq, work)
        
        self.dx_dq = Df_Dq
        self.dPx_dq = DPf_Dq + DQ_Dq
        
        # Calculate the precition hessian for the PEM
        if self.pem == 'hess':
            self._calculate_prediction_hess()
    
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
    
    def __init__(self, model, **options):
        # Initialize base
        super().__init__(model, **options)
        
        # Get transform options
        ut_options = options.copy()
        ut_options.update(utils.extract_subkeys(options, 'corr_ut_'))
        
        # Select transform class
        sqrt = ut_options.get('sqrt', 'cholesky')
        if sqrt == 'cholesky':
            UTClass = CholeskyUnscentedTransform
        elif sqrt == 'svd':
            UTClass = SVDUnscentedTransform
        else:
            raise ValueError("Invalid value for `sqrt` option.")
        
        # Create the transform object
        self.__ut = UTClass(model.nx, **ut_options)
    
    def initialize_history(self, size):
        size_changed = size != self.history_size
        super().initialize_history(size)

        # The extra variables are only needed for the PEM.
        if self.pem != 'save':
            return
        
        # Allocate the history arrays, if needed        
        if size_changed:
            nx = self.model.nx
            ny = self.model.ny
            nq = self.model.nq
            base_shape = self.base_shape
            self.y_active = np.zeros((size, ny), dtype=bool)
            self.e = np.zeros((size,) + base_shape + (ny,))
            self.K = np.zeros((size,) + base_shape + (nx, ny))
            self.Pxh = np.zeros((size,) + base_shape + (nx, ny))
            self.Py = np.zeros((size,) + base_shape + (ny, ny))
            self.PyI = np.zeros((size,) + base_shape + (ny, ny))
            self.PyC = np.zeros((size,) + base_shape + (ny, ny))

    def _save_correction_pem(self, active, e, K, Pxh, Py, PyI, PyC):
        k = self.k
        cov_ind = (k, ...) + np.ix_(active, active)
        self.y_active[k] = active
        self.e[k, ..., active] = e
        self.K[k, ..., active] = K
        self.Pxh[k, ..., active] = Pxh
        self.Py[cov_ind] = Py
        self.PyI[cov_ind] = PyI
        self.PyC[cov_ind] = PyC
    
    def correct(self, y):
        '''Correct the state distribution, given the measurement vector.'''
        assert np.shape(y) == (self.model.ny,), "No vectorization accepted in y"

        mask = ma.getmaskarray(y)
        if np.all(mask):
            return
        
        # Remove inactive outputs
        active = ~mask
        y = ma.compressed(y)
        R = self.model.R()[np.ix_(active, active)]
        def h_fun(x):
            return self.model.h(self.k, x)[..., active]
        
        # Perform unscented transform
        h, Ph = self.__ut.transform(h_fun, self.x, self.Px)
        Pxh = self.__ut.crosscov()
        
        # Factorize covariance
        Py = Ph + R
        PyC = numpy.linalg.cholesky(Py)
        PyCI = numpy.linalg.inv(PyC)
        PyI = np.einsum('...ki,...kj', PyCI, PyCI)
        
        # Perform correction
        e = y - h
        K = np.einsum('...ik,...kj', Pxh, PyI)
        x_corr = self.x + np.einsum('...ij,...j', K, e)
        Px_corr = self.Px - np.einsum('...ik,...jl,...lk', K, K, Py)
    
        # Update log-likelihood and save PEM data
        if self.pem:
            PyCD = np.einsum('...kk->...k', PyC)
            self.L -= 0.5 * np.einsum('...i,...ij,...j', e, PyI, e) 
            self.L -= np.log(PyCD).sum(-1)
        if self.pem == 'save':
            self._save_correction_pem(active, e, K, Pxh, Py, PyI, PyC)
        elif self.pem == 'grad' or self.pem == 'hess':
            self._calculate_correction_grad(active, e, K, Pxh, Py, PyI, PyC)

        # Save the correction data
        self._save_correction(x_corr, Px_corr)
    
    def _calculate_correction_grad(self, active, e, K, Pxh, Py, PyI, PyC):
        k = self.k
        x = self.x
        dx_dq = self.dx_dq
        dPx_dq = self.dPx_dq
        dR_dq = self.model.dR_dq()[(...,) + np.ix_(active, active)]
        
        def Dh_Dq_fun(x, dx_dq):
            dh_dq = self.model.dh_dq(k, x)[..., active]
            dh_dx = self.model.dh_dx(k, x)[..., active]
            return dh_dq + np.einsum('...qx,...xh->...qh', dx_dq, dh_dx)
        ut_grads = self.__ut.transform_diff(Dh_Dq_fun, dx_dq, dPx_dq, True)
        Dh_Dq, dPh_dq, dPxh_dq = ut_grads
        
        de_dq = -Dh_Dq
        dPy_dq = dPh_dq + dR_dq
        dPyI_dq = -np.einsum('...ij,...ajk,...kl', PyI, dPy_dq, PyI)
        dK_dq = np.einsum('...ik,...akj', Pxh, dPyI_dq)
        dK_dq += np.einsum('...aik,...kj', dPxh_dq, PyI)
        
        self.dx_dq += np.einsum('...aij,...j', dK_dq, e)
        self.dx_dq += np.einsum('...ij,...aj', K, de_dq)
        self.dPx_dq -= np.einsum('...aik,...jl,...lk', dK_dq, K, Py)
        self.dPx_dq -= np.einsum('...ik,...ajl,...lk', K, dK_dq, Py)
        self.dPx_dq -= np.einsum('...ik,...jl,...alk', K, K, dPy_dq)

        dPyC_dq = cholesky_sqrt_diff(PyC, dPy_dq)
        diag_PyC = np.einsum('...kk->...k', PyC)
        diag_dPyC_dq = np.einsum('...kk->...k', dPyC_dq)
        self.dL_dq -= np.sum(diag_dPyC_dq / diag_PyC, axis=-1)
        self.dL_dq -= 0.5 * np.einsum('...ai,...ij,...j', de_dq, PyI, e)
        self.dL_dq -= 0.5 * np.einsum('...i,...aij,...j', e, dPyI_dq, e)
        self.dL_dq -= 0.5 * np.einsum('...i,...ij,...aj', e, PyI, de_dq)


class DTUnscentedKalmanFilter(DTUnscentedPredictor, DTUnscentedCorrector):
    pass

