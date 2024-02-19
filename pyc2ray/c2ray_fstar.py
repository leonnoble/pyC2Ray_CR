import numpy as np, pandas as pd
from astropy.cosmology import FlatLambdaCDM
from glob import glob
import astropy.constants as cst
import tools21cm as t2c
import h5py, os
try: from yaml import CSafeLoader as SafeLoader
except ImportError: from yaml import SafeLoader

from .utils.logutils import printlog
from .evolve import evolve3D
from .asora_core import device_init, device_close, photo_table_to_device
from .radiation import BlackBodySource, make_tau_table
from .utils.other_utils import get_extension_in_folder, get_redshifts_from_output, find_bins

from .utils import get_source_redshifts
from .c2ray_base import C2Ray, YEAR, Mpc, msun2g, ev2fr, ev2k

from .source_model import *

__all__ = ['C2Ray_fstar']

# m_p = 1.672661e-24

# ======================================================================
# This file contains the C2Ray_CubeP3M subclass of C2Ray, which is a
# version used for simulations that read in N-Body data from CubeP3M
# ======================================================================

class C2Ray_fstar(C2Ray):
    def __init__(self,paramfile,Nmesh,use_gpu,use_mpi):
        """Basis class for a C2Ray Simulation

        Parameters
        ----------
        paramfile : str
            Name of a YAML file containing parameters for the C2Ray simulation
        Nmesh : int
            Mesh size (number of cells in each dimension)
        use_gpu : bool
            Whether to use the GPU-accelerated ASORA library for raytracing

        """
        super().__init__(paramfile, Nmesh, use_gpu, use_mpi)
        if(self.rank == 0):
            self.printlog('Running: "C2Ray for %d Mpc/h volume"' %self.boxsize)

    # =====================================================================================================
    # USER DEFINED METHODS
    # =====================================================================================================

    def fstar_model(self, mhalo):
        kind = self.fstar_kind
        if kind.lower() in ['fgamma', 'f_gamma', 'mass_independent']: 
            fstar = (self.cosmology.Ob0/self.cosmology.Om0)
        elif kind.lower() in ['dpl', 'mass_dependent']:
            model = SourceModel(
                        Nion=self.fstar_dpl['Nion'],
                        f0=self.fstar_dpl['f0'], 
                        Mt=self.fstar_dpl['Mt'], 
                        Mp=self.fstar_dpl['Mp'],
                        g1=self.fstar_dpl['g1'], 
                        g2=self.fstar_dpl['g2'], 
                        g3=self.fstar_dpl['g3'], 
                        g4=self.fstar_dpl['g4'],
                        f0_esc=self.fstar_dpl['f0_esc'],
                        Mp_esc=self.fstar_dpl['Mp_esc'],
                        al_esc=self.fstar_dpl['al_esc'])
            fstar = model.f_star.deterministic(mhalo)['fstar']
            fesc = model.f_esc.deterministic(mhalo)['fesc']
        else:
            print(f'{kind} fstar model is not implemented.')
        return fstar, fesc

    def ionizing_flux(self, file, z, save_Mstar=False): # >:( trgeoip
        """Read sources from a C2Ray-formatted file
        Parameters
        ----------
        file : str
            Filename to read.
        ts : float
            time-step in Myrs.
        kind: str
            The kind of source model to use.
        
        Returns
        -------
        srcpos : array
            Grid positions of the sources formatted in a suitable way for the chosen raytracing algorithm
        normflux : array
            Normalization of the flux of each source (relative to S_star)
        """
        box_len, n_grid = self.boxsize, self.N
        
        srcpos_mpc, srcmass_msun = self.read_haloes(self.sources_basename+file, box_len)
        fstar, fesc = self.fstar_model(srcmass_msun)
        mstar_msun = fesc*fstar*srcmass_msun

        h = self.cosmology.h
        hg = Halo2Grid(box_len=box_len/h, n_grid=n_grid)
        hg.set_halo_pos(srcpos_mpc, unit='mpc')
        hg.set_halo_mass(mstar_msun, unit='Msun')

        binned_mstar, bin_edges, bin_num = hg.value_on_grid(hg.pos_grid, mstar_msun)
        srcpos, srcmstar = hg.halo_value_on_grid(mstar_msun, binned_value=binned_mstar)
        
        if save_Mstar:
            folder_path = save_Mstar
            fname_hdf5 = folder_path+f'/{z:.3f}-Mstar_sources.hdf5'
            if not os.path.exists(folder_path):
                os.makedirs(folder_path)
                print(f"Folder '{folder_path}' created successfully.")
            else:
                pass
                # print(f"Folder '{folder_path}' already exists.")
            # Create HDF5 file from the data
            with h5py.File(fname_hdf5,"w") as f:
                # Store Data
                dset_pos = f.create_dataset("sources_positions", data=srcpos)
                dset_mass = f.create_dataset("sources_mass", data=srcmstar)
                # Store Metadata
                f.attrs['z'] = z
                f.attrs['h'] = self.cosmology.h
                f.attrs['numhalo'] = srcmstar.shape[0]
                f.attrs['units'] = 'cMpc   Msun'
        
        S_star_ref = 1e48

        # source life-time in cgs
        ts = 1. / (self.alph_h * (1+z) * self.cosmology.H(z=z).cgs.value)

        # normalize flux
        normflux = msun2g * self.fstar_dpl['Nion'] * srcmstar / (m_p * ts * S_star_ref)
        if(self.rank == 0):
            self.printlog('\n---- Reading source file with total of %d ionizing source:\n%s' %(normflux.size, file))
            self.printlog(' Total Flux : %e [1/s]' %np.sum(normflux*S_star_ref))
            self.printlog(' Source lifetime : %f Myr' %(ts/(1e6*YEAR)))
            self.printlog(' min, max stellar (grid) mass : %.3e  %.3e [Msun] and min, mean, max number of ionising sources : %.3e  %.3e  %.3e [1/s]' %(srcmstar.min(), srcmstar.max(), normflux.min()*S_star_ref, normflux.mean()*S_star_ref, normflux.max()*S_star_ref))
            return srcpos, normflux
    
    def read_haloes(self, halo_file, box_len): # >:( trgeoip
        """Read haloes from a file.

        Parameters
        ----------
        halo_file : str
            Filename to read
        
        Returns
        -------
        srcpos_mpc : array
            Positions of the haloes in Mpc.
        srcmass_msun : array
            Masses of the haloes in Msun.
        """

        if(halo_file.endswith('.hdf5')):
            # Read haloes from a CUBEP3M file format converted in hdf5.
            f = h5py.File(halo_file)
            h = f.attrs['h']
            srcmass_msun = f['mass'][:]/h #Msun
            srcpos_mpc = f['pos'][:]/h   #Mpc
            f.close()
        elif(halo_file.endswith('.dat')):
            # Read haloes from a CUBEP3M file format.
            hl = t2c.HaloCubeP3MFull(filename=halo_file, box_len=box_len)
            h  = self.h
            srcmass_msun = hl.get(var='m')/h   #Msun
            srcpos_mpc  = hl.get(var='pos')/h #Mpc
        elif(halo_file.endswith('.txt')):
            # Read haloes from a PKDGrav converted in txt.
            hl = np.loadtxt(halo_file)
            srcmass_msun = hl[:,0]/self.cosmology.h # Msun
            srcpos_mpc = (hl[:,1:]+self.boxsize/2) # Mpc/h

            # apply periodic boundary condition shift
            srcpos_mpc[srcpos_mpc > self.boxsize] = self.boxsize - srcpos_mpc[srcpos_mpc > self.boxsize]
            srcpos_mpc[srcpos_mpc < 0.] = self.boxsize + srcpos_mpc[srcpos_mpc < 0.]
            srcpos_mpc /= self.cosmology.h # Mpc

        return srcpos_mpc, srcmass_msun
        
    def read_density(self, fbase, z=None):
        """ Read coarser density field from C2Ray-formatted file

        This method is meant for reading density field run with either N-body or hydro-dynamical simulations. The field is then smoothed on a coarse mesh grid.

        Parameters
        ----------
        fbase : string
            the file name (without the path) of the file to open
        
        """
        file = self.density_basename+fbase
        rdr = t2c.Pkdgrav3data(self.boxsize, self.N, Omega_m=self.cosmology.Om0)
        self.ndens = self.cosmology.critical_density0.cgs.value * self.cosmology.Ob0 * (1.+rdr.load_density_field(file)) / (self.mean_molecular * m_p) * (1+z)**3
        if(self.rank == 0):
            self.printlog('\n---- Reading density file:\n  %s' %file)
            self.printlog(' min, mean and max density : %.3e  %.3e  %.3e [1/cm3]' %(self.ndens.min(), self.ndens.mean(), self.ndens.max()))


    def read_clumping(self, parfile, z=None):
        """ Read coarser clumping factor

        This method is meant for reading clumping files or compute them with Bianco+ (2021) method.

        Parameters
        ----------
        n : int
            Number of sources to read from the file
        
        """
        
        # get parameter files
        df = pd.read_csv(parfile, index_col=0)

        # find nearest redshift bin
        zlow, zhigh = find_bins(z, df.index)

        # calculate weight to 
        w_l, w_h = (z-zlow)/(zhigh - zlow), (zhigh-z)/(zhigh - zlow)
        
        # get parameters weighted
        a, b, c = (df.loc[zlow]*w_l + df.loc[zhigh]*w_h).to_numpy()

        # compute clumping factor
        x = np.log(1 + self.ndens / self.ndens.mean())
        clump = 10**(a*x**2 + b*x**2 + c)

        # combine clumping and density field
        self.ndens *= clump

        if(self.rank == 0):
            self.printlog('\n---- Created Clumping Factor :')
            self.printlog(' min, mean and max clumping : %.3e  %.3e  %.3e' %(clump.min(), clump.mean(), clump.max()))
            self.printlog(' min, mean and max density : %.3e  %.3e  %.3e' %(self.ndens.min(), self.ndens.mean(), self.ndens.max()))
        return clump

    # =====================================================================================================
    # Below are the overridden initialization routines specific to the CubeP3M case
    # =====================================================================================================

    def _redshift_init(self):
        """Initialize time and redshift counter
        """
        self.zred_density = np.loadtxt(self.density_basename+'redshift_density.txt')
        self.zred_sources = np.loadtxt(self.sources_basename+'redshift_sources.txt')
        if(self.resume):
            # get the resuming redshift
            self.zred = np.min(get_redshifts_from_output(self.results_basename))
            _, self.prev_zdens = find_bins(self.zred, self.zred_density)
            _, self.prev_zsourc = find_bins(self.zred, self.zred_sources)
        else:
            self.prev_zdens = -1
            self.prev_zsourc = -1
            self.zred = self.zred_0

        self.time = self.zred2time(self.zred)

    def _material_init(self):
        """Initialize material properties of the grid
        """
        if(self.resume):
            # get fields at the resuming redshift
            self.ndens = self.read_density(fbase='CDM_200Mpc_2048.%05d.den.256.0' %self.resume, z=self.prev_zdens)
            
            # get extension of the output file
            ext = get_extension_in_folder(path=self.results_basename)
            if(ext == '.dat'):
                self.xh = t2c.read_cbin(filename='%sxfrac_%.3f.dat' %(self.results_basename, self.zred), bits=64, order='F')
                self.phi_ion = t2c.read_cbin(filename='%sIonRates_%.3f.dat' %(self.results_basename, self.zred), bits=32, order='F')
            elif(ext == '.npy'):
                self.xh = np.load('%sxfrac_%.3f.npy' %(self.results_basename, self.zred))
                self.phi_ion = np.load('%sIonRates_%.3f.npy' %(self.results_basename, self.zred))

            # TODO: implement heating
            temp0 = self._ld['Material']['temp0']
            self.temp = temp0 * np.ones(self.shape, order='F')
        else:
            xh0 = self._ld['Material']['xh0']
            temp0 = self._ld['Material']['temp0']
            avg_dens = self._ld['Material']['avg_dens']

            self.ndens = avg_dens * np.empty(self.shape, order='F')
            self.xh = xh0 * np.ones(self.shape, order='F')
            self.temp = temp0 * np.ones(self.shape, order='F')
            self.phi_ion = np.zeros(self.shape, order='F')

    def _sources_init(self):
        """Initialize settings to read source files
        """
        self.fstar_kind = self._ld['Sources']['fstar_kind']
        if(self.fstar_kind == 'fgamma'):
            self.fgamma_hm = self._ld['Sources']['fgamma_hm']
            self.fgamma_lm = self._ld['Sources']['fgamma_lm']
            if(self.rank == 0):
                self.printlog(f"Using UV model with fgamma_lm = {self.fgamma_lm:.1f} and fgamma_hm = {self.fgamma_hm:.1f}")
        elif(self.fstar_kind == 'dpl'):
            self.fstar_dpl = {
                            'Nion': self._ld['Sources']['Nion'],
                            'f0': self._ld['Sources']['f0'],
                            'Mt': self._ld['Sources']['Mt'], 
                            'Mp': self._ld['Sources']['Mp'], 
                            'g1': self._ld['Sources']['g1'], 
                            'g2': self._ld['Sources']['g2'], 
                            'g3': self._ld['Sources']['g3'], 
                            'g4': self._ld['Sources']['g4'], 
                            'f0_esc': self._ld['Sources']['f0_esc'], 
                            'Mp_esc': self._ld['Sources']['Mp_esc'], 
                            'al_esc': self._ld['Sources']['al_esc']
                            }
            if(self.rank == 0):
                self.printlog(f"Using {self.fstar_kind} to model the stellar-to-halo relation, and the parameter dictionary = {self.fstar_dpl}.")

            self.acc_model = self._ld['Sources']['accretion_model']
            self.alph_h = self._ld['Sources']['alpha_h']