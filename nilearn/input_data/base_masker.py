"""
Transformer used to apply basic transformations on MRI data.
"""
# Author: Gael Varoquaux, Alexandre Abraham
# License: simplified BSD

import warnings

import numpy as np
import itertools

from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.externals.joblib import Memory, Parallel, delayed

from .. import masking
from .. import image
from .. import signal
from .. import _utils
from .._utils.cache_mixin import CacheMixin, cache
from .._utils.class_inspect import enclosing_scope_name, get_params


def filter_and_mask(niimgs, mask_img_,
                    parameters,
                    ref_memory_level=0,
                    memory=Memory(cachedir=None),
                    verbose=0,
                    confounds=None,
                    reference_affine=None,
                    copy=True):
    # If we have a string (filename), we won't need to copy, as
    # there will be no side effect

    if isinstance(niimgs, basestring):
        copy = False
    
    niimgs = _utils.check_niimgs(niimgs, accept_3d=True)

    # If there is a reference affine, we may have to force resampling
    target_affine = parameters['target_affine']
    if (target_affine is None and reference_affine is not None
                and reference_affine.shape == niimgs.get_affine().shape
                and not np.allclose(niimgs.get_affine(), reference_affine)):
        warnings.warn('Affine is different across subjects.'
                      ' Realignement on first subject affine forced')
        target_affine = reference_affine

    if verbose > 0:
        class_name = enclosing_scope_name(stack_level=2)

    # Resampling: allows the user to change the affine, the shape or both
    if verbose > 1:
        print("[%s] Resampling" % class_name)

    niimgs = cache(image.resample_img, memory, ref_memory_level,
                   memory_level=2, ignore=['copy'])(
                       niimgs,
                       target_affine=target_affine,
                       target_shape=parameters['target_shape'],
                       copy=copy)

    # Load data (if filenames are given, load them)
    if verbose > 0:
        print("[%s] Loading data from %s" % (
            class_name,
            _utils._repr_niimgs(niimgs)[:200]))

    # Get series from data with optional smoothing
    if verbose > 1:
        print("[%s] Masking and smoothing" % class_name)
    data = masking.apply_mask(niimgs, mask_img_,
                              smoothing_fwhm=parameters['smoothing_fwhm'])

    # Temporal
    # ========
    # Detrending (optional)
    # Filtering
    # Confounds removing (from csv file or numpy array)
    # Normalizing

    if verbose > 1:
        print("[%s] Cleaning signal" % class_name)
    if not 'sessions' in parameters or parameters['sessions'] is None:
        clean_memory_level = 2
        if (parameters['high_pass'] is not None
                and parameters['low_pass'] is not None):
            clean_memory_level = 4

        data = cache(signal.clean, memory, ref_memory_level,
                     memory_level=clean_memory_level)(
                         data,
                         confounds=confounds, low_pass=parameters['low_pass'],
                         high_pass=parameters['high_pass'],
                         t_r=parameters['t_r'],
                         detrend=parameters['detrend'],
                         standardize=parameters['standardize'])
    else:
        sessions = parameters['sessions']
        for s in np.unique(sessions):
            if confounds is not None:
                confounds = confounds[sessions == s]
            data[:, sessions == s] = \
                cache(signal.clean, memory, ref_memory_level, memory_level=2)(
                    data[:, sessions == s],
                    confounds=confounds,
                    low_pass=parameters['low_pass'],
                    high_pass=parameters['high_pass'],
                    t_r=parameters['t_r'],
                    detrend=parameters['detrend'],
                    standardize=parameters['standardize']
                )

    # For _later_: missing value removal or imputing of missing data
    # (i.e. we want to get rid of NaNs, if smoothing must be done
    # earlier)
    # Optionally: 'doctor_nan', remove voxels with NaNs, other option
    # for later: some form of imputation

    return data, niimgs.get_affine()


class BaseMasker(BaseEstimator, TransformerMixin, CacheMixin):
    """Base class for NiftiMaskers
    """

    def transform_single_niimgs(self, niimgs, confounds=None, copy=True):
        if not hasattr(self, 'mask_img_'):
            raise ValueError('It seems that %s has not been fit. '
                             'You must call fit() before calling transform().'
                             % self.__class__.__name__)
        from .nifti_masker import NiftiMasker
        params = get_params(NiftiMasker, self)
        # Remove the mask-computing params: they are not useful and will
        # just invalid the cache for no good reason
        for name in ('mask', 'mask_connected', 'mask_lower_cutoff',
                     'mask_opening', 'mask_upper_cutoff'):
            params.pop(name, None)
        data, _ = self._cache(filter_and_mask, memory_level=1,
                           ignore=['verbose', 'memory', 'copy'])(
                              niimgs, self.mask_img_,
                              params,
                              ref_memory_level=self.memory_level,
                              memory=self.memory,
                              verbose=self.verbose,
                              confounds=confounds,
                              copy=copy
                            )
        return data

    def transform_niimgs(self, niimgs_list, confounds=None, copy=True, n_jobs=1):
        # Transform several niimgs un parallel. This could replace the previous
        # function
        if not hasattr(self, 'mask_img_'):
            raise ValueError('It seems that %s has not been fit. '
                             'You must call fit() before calling transform().'
                             % self.__class__.__name__)
        from .nifti_masker import NiftiMasker
        params = get_params(NiftiMasker, self)

        reference_affine = None
        if self.target_affine is None:
            # Load the first image and use it as a reference for all other
            # subjects
            reference_affine = _utils.check_niimgs(niimgs_list[0]).get_affine()

        func = self._cache(filter_and_mask, memory_level=1,
                           ignore=['verbose', 'memory', 'copy'])
        if confounds is None:
            confounds = itertools.repeat(None, len(niimgs_list))
        data = Parallel(n_jobs=n_jobs)(delayed(func)(
                              niimgs, self.mask_img_,
                              params,
                              ref_memory_level=self.memory_level,
                              memory=self.memory,
                              verbose=self.verbose,
                              confounds=confounds,
                              reference_affine=reference_affine,
                              copy=copy)
                          for niimgs, confounds in zip(niimgs_list, confounds))
        return zip(*data)[0]

    def fit_transform(self, X, y=None, confounds=None, **fit_params):
        """Fit to data, then transform it

        Fits transformer to X and y with optional parameters fit_params
        and returns a transformed version of X.

        Parameters
        ----------
        X : numpy array of shape [n_samples, n_features]
            Training set.

        y : numpy array of shape [n_samples]
            Target values.

        Returns
        -------
        X_new : numpy array of shape [n_samples, n_features_new]
            Transformed array.

        """
        # non-optimized default implementation; override when a better
        # method is possible for a given clustering algorithm
        if y is None:
            # fit method of arity 1 (unsupervised transformation)
            if self.mask is None:
                return self.fit(X, **fit_params
                                ).transform(X, confounds=confounds)
            else:
                return self.fit(**fit_params).transform(X, confounds=confounds)
        else:
            # fit method of arity 2 (supervised transformation)
            if self.mask is None:
                return self.fit(X, y, **fit_params
                                ).transform(X, confounds=confounds)
            else:
                warnings.warn('[%s.fit] Generation of a mask has been'
                              ' requested (y != None) while a mask has'
                              ' been provided at masker creation. Given mask'
                              ' will be used.' % self.__class__.__name__)
                return self.fit(**fit_params).transform(X, confounds=confounds)

    def inverse_transform(self, X):
        return self._cache(masking.unmask, memory_level=1,
            )(X, self.mask_img_)
