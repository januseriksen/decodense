#!/usr/bin/env python
# -*- coding: utf-8 -*

"""
orbitals module
"""

__author__ = 'Dr. Janus Juul Eriksen, University of Bristol, UK'
__maintainer__ = 'Dr. Janus Juul Eriksen'
__email__ = 'janus.eriksen@bristol.ac.uk'
__status__ = 'Development'

import multiprocessing as mp
import numpy as np
from pyscf import gto, scf, dft, lo, lib
from typing import List, Tuple, Dict, Union, Any

from .tools import make_rdm1, contract

LOC_CONV = 1.e-10


def loc_orbs(mol: gto.Mole, mo_coeff: Tuple[np.ndarray, np.ndarray], \
             s: np.ndarray, ref: str, variant: str) -> np.ndarray:
        """
        this function returns a set of localized MOs of a specific variant
        """
        # loop over spins
        for i, spin_mo in enumerate((mol.alpha, mol.beta)):

            if variant == 'fb':
                # foster-boys procedure
                loc = lo.Boys(mol, mo_coeff[i][:, spin_mo])
                loc.conv_tol = LOC_CONV
                # FB MOs
                mo_coeff[i][:, spin_mo] = loc.kernel()
            elif variant == 'pm':
                # pipek-mezey procedure
                loc = lo.PM(mol, mo_coeff[i][:, spin_mo])
                loc.conv_tol = LOC_CONV
                # PM MOs
                mo_coeff[i][:, spin_mo] = loc.kernel()
            elif 'ibo' in variant:
                # orthogonalized IAOs
                iao = lo.iao.iao(mol, mo_coeff[i][:, spin_mo])
                iao = lo.vec_lowdin(iao, s)
                # IBOs
                mo_coeff[i][:, spin_mo] = lo.ibo.ibo(mol, mo_coeff[i][:, spin_mo], iaos=iao, \
                                                     grad_tol = LOC_CONV, exponent=int(variant[-1]), verbose=0)
            # closed-shell reference
            if ref == 'restricted' and mol.spin == 0:
                mo_coeff[i+1][:, spin_mo] = mo_coeff[i][:, spin_mo]
                break

        return mo_coeff


def assign_rdm1s(mol: gto.Mole, s: np.ndarray, mo_coeff: Tuple[np.ndarray, np.ndarray], \
                 mo_occ: np.ndarray, ref: str, pop: str, part: str, multiproc: bool, verbose: int, \
                 **kwargs: float) -> Tuple[Union[List[np.ndarray], List[List[np.ndarray]]], Union[None, np.ndarray]]:
        """
        this function returns a list of population weights of each spin-orbital on the individual atoms
        """
        # declare nested kernel function in global scope
        global get_weights

        # max number of occupied spin-orbs
        n_spin = max(mol.alpha.size, mol.beta.size)

        # mol object projected into minao basis
        if pop == 'iao':
            pmol = lo.iao.reference_mol(mol)
        else:
            pmol = mol

        # number of atoms
        natm = pmol.natm

        # AO labels
        ao_labels = pmol.ao_labels(fmt=None)

        # overlap matrix
        if pop == 'mulliken':
            ovlp = s
        else:
            ovlp = np.eye(pmol.nao_nr())

        def get_weights(orb_idx: int):
            """
            this function computes the full set of population weights
            """
            # get orbital
            orb = mo[:, orb_idx].reshape(mo.shape[0], 1)
            # orbital-specific rdm1
            rdm1_orb = make_rdm1(orb, mocc[orb_idx])
            # population weights of rdm1_orb
            return _population(natm, ao_labels, ovlp, rdm1_orb)

        # init population weights array
        weights = [np.zeros([n_spin, pmol.natm], dtype=np.float64), np.zeros([n_spin, pmol.natm], dtype=np.float64)]

        # loop over spin
        for i, spin_mo in enumerate((mol.alpha, mol.beta)):

            # get mo coefficients and occupation
            if pop == 'mulliken':
                mo = mo_coeff[i][:, spin_mo]
            elif pop == 'iao':
                iao = lo.iao.iao(mol, mo_coeff[i][:, spin_mo])
                iao = lo.vec_lowdin(iao, s)
                mo = contract('ki,kl,lj->ij', iao, s, mo_coeff[i][:, spin_mo])
            mocc = mo_occ[i][spin_mo]

            # domain
            domain = np.arange(spin_mo.size)
            # execute kernel
            if multiproc:
                n_threads = min(domain.size, lib.num_threads())
                with mp.Pool(processes=n_threads) as pool:
                    weights[i] = pool.map(get_weights, domain) # type:ignore
            else:
                weights[i] = list(map(get_weights, domain)) # type:ignore

            # closed-shell reference
            if ref == 'restricted' and mol.spin == 0:
                weights[i+1] = weights[i]
                break

        # verbose print
        if 0 < verbose:
            symbols = [pmol.atom_pure_symbol(i) for i in range(pmol.natm)]
            print('\n *** partial population weights: ***')
            print(' spin  ' + 'MO       ' + '      '.join(['{:}'.format(i) for i in symbols]))
            for i, spin_mo in enumerate((mol.alpha, mol.beta)):
                for j in domain:
                    with np.printoptions(suppress=True, linewidth=200, formatter={'float': '{:6.3f}'.format}):
                        print('  {:s}    {:>2d}   {:}'.format('a' if i == 0 else 'b', spin_mo[j], weights[i][j]))

        # bond-wise partitioning
        if part == 'bonds':
            # init population centres array and get threshold
            centres = [np.zeros([mol.alpha.size, 2], dtype=np.int), np.zeros([mol.beta.size, 2], dtype=np.int)]
            thres = kwargs['thres']
            # loop over spin
            for i, spin_mo in enumerate((mol.alpha, mol.beta)):
                # loop over orbitals
                for j in domain:
                    # get sorted indices
                    max_idx = np.argsort(weights[i][j])[::-1]
                    # compute population centres
                    if np.abs(weights[i][j][max_idx[0]]) > thres:
                        # core orbital or lone pair
                        centres[i][j] = np.array([max_idx[0], max_idx[0]], dtype=np.int)
                    else:
                        # valence orbitals
                        centres[i][j] = np.sort(np.array([max_idx[0], max_idx[1]], dtype=np.int))
                # closed-shell reference
                if ref == 'restricted' and mol.spin == 0:
                    centres[i+1] = centres[i]
                    break
            # unique and repetitive centres
            centres_unique = np.array([np.unique(centres[i], axis=0) for i in range(2)])
            rep_idx = [[np.where((centres[i] == j).all(axis=1))[0] for j in centres_unique[i]] for i in range(2)]

        if part in ['atoms', 'eda']:
            return weights, None
        else:
            return rep_idx, centres_unique


def _population(natm: int, ao_labels: np.ndarray, ovlp: np.ndarray, rdm1: np.ndarray) -> np.ndarray:
        """
        this function returns the mulliken populations on the individual atoms
        """
        # mulliken population matrix
        pop = contract('ij,ji->i', rdm1, ovlp)
        # init populations
        populations = np.zeros(natm)

        # loop over AOs
        for i, k in enumerate(ao_labels):
            populations[k[0]] += pop[i]

        return populations


