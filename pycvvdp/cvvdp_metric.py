from abc import abstractmethod
from urllib.parse import ParseResultBytes
from numpy.lib.shape_base import expand_dims
import math
import torch
from torch.utils import checkpoint
from torch.functional import Tensor
import torch.nn.functional as Func
import numpy as np 
import os
import sys
import json
#import argparse
#import time
#import math
import torch.utils.benchmark as torchbench
import logging

from pycvvdp.visualize_diff_map import visualize_diff_map
from pycvvdp.video_source import *

from pycvvdp.vq_metric import *
from pycvvdp.colorspace import lms2006_to_dkld65

# For debugging only
# from gfxdisp.pfs.pfs_torch import pfs_torch

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from third_party.cpuinfo import cpuinfo
from pycvvdp.lpyr_dec import lpyr_dec, weber_contrast_pyr, log_contrast_pyr
from interp import interp1, interp3

import pycvvdp.utils as utils

#from utils import *
#from fvvdp_test import FovVideoVDP_Testbench

from pycvvdp.display_model import vvdp_display_photometry, vvdp_display_geometry
from pycvvdp.csf import castleCSF

"""
ColourVideoVDP metric. Refer to pytorch_examples for examples on how to use this class. 
"""
class cvvdp(vq_metric):
    def __init__(self, display_name="standard_4k", display_photometry=None, display_geometry=None, color_space="sRGB", heatmap=None, quiet=False, device=None, temp_padding="replicate", use_checkpoints=False, calibrated_ckpt=None):
        self.quiet = quiet
        self.heatmap = heatmap
        self.color_space = color_space
        self.temp_padding = temp_padding
        self.use_checkpoints = use_checkpoints # Used for training

        self.do_heatmap = (not self.heatmap is None) and (self.heatmap != "none")

        # Use GPU if available
        if device is None:
            if torch.cuda.is_available() and torch.cuda.device_count()>0:
                self.device = torch.device('cuda:0')
            else:
                self.device = torch.device('cpu')
        else:
            self.device = device
        
        self.set_display_model(display_name, display_photometry=display_photometry, display_geometry=display_geometry)

        self.load_config()
        if calibrated_ckpt is not None:
            self.update_from_checkpoint(calibrated_ckpt)

        # if self.mask_s > 0.0:
        #     self.mask_p = self.mask_q + self.mask_s

        # self.csf_cache              = {}
        # self.csf_cache_dirs         = [
        #                                 "csf_cache",
        #                                 os.path.join(os.path.dirname(__file__), "csf_cache"),
        #                               ]

        # self.omega = torch.tensor([0,5], device=self.device, requires_grad=False)
        # for oo in self.omega:
        #     self.preload_cache(oo, self.csf_sigma)

        self.heatmap_pyr = None

    def load_config( self ):

        #parameters_file = os.path.join(os.path.dirname(__file__), "fvvdp_data/fvvdp_parameters.json")
        self.parameters_file = utils.config_files.find( "cvvdp_parameters.json" )
        logging.debug( f"Loading ColourVideoVDP parameters from '{self.parameters_file}'" )
        parameters = utils.json2dict(self.parameters_file)

        #all common parameters between Matlab and Pytorch, loaded from the .json file
        self.mask_p = parameters['mask_p']
        self.mask_c = parameters['mask_c'] # content masking adjustment
        self.pu_dilate = parameters['pu_dilate']
        self.beta = parameters['beta'] # The exponent of the spatial summation (p-norm)
        self.beta_t = parameters['beta_t'] # The exponent of the summation over time (p-norm)
        self.beta_tch = parameters['beta_tch'] # The exponent of the summation over temporal channels (p-norm)
        self.beta_sch = parameters['beta_sch'] # The exponent of the summation over spatial channels (p-norm)
        self.csf_sigma = parameters['csf_sigma']
        self.sensitivity_correction = parameters['sensitivity_correction'] # Correct CSF values in dB. Negative values make the metric less sensitive.
        self.masking_model = parameters['masking_model']
        self.local_adapt = parameters['local_adapt'] # Local adaptation: 'simple' or or 'gpyr'
        self.contrast = parameters['contrast']  # One of: 'weber_g0_ref', 'weber_g1_ref', 'weber_g1', 'log'
        self.jod_a = parameters['jod_a']
        self.jod_exp = parameters['jod_exp']
        self.mask_q_sust = parameters['mask_q_sust']
        self.mask_q_trans = parameters['mask_q_trans']
        self.filter_len = parameters['filter_len']
        self.ch_weights = torch.as_tensor( parameters['ch_weights'], device=self.device ) # Per-channel weight, Y-sust, rg, vy, Y-trans
        self.sigma_tf = torch.as_tensor( parameters['sigma_tf'], device=self.device ) # Temporal filter params, per-channel: Y-sust, rg, vy, Y-trans
        self.beta_tf = torch.as_tensor( parameters['beta_tf'], device=self.device ) # Temporal filter params, per-channel: Y-sust, rg, vy, Y-trans
        self.baseband_weight = parameters['baseband_weight']
        self.dclamp_type = parameters['dclamp_type']
        self.dclamp_par = torch.as_tensor( parameters['dclamp_par'], device=self.device ) # Clamping of difference values
        self.version = parameters['version']

        self.omega = [0, 5]

        self.csf = castleCSF(contrast=self.contrast, device=self.device)

        # other parameters
        self.debug = False

    def update_from_checkpoint(self, ckpt):
        assert os.path.isfile(ckpt), f'Calibrated PyTorch checkpoint not found at: {ckpt}'
        # Read relevant parameters from state_dict
        prefix = 'params.'
        for key, value in torch.load(ckpt)['state_dict'].items():
            if key.startswith(prefix):
                setattr(self, key[len(prefix):], value.to(self.device))

    def set_display_model(self, display_name="standard_4k", display_photometry=None, display_geometry=None):
        if display_photometry is None:
            self.display_photometry = vvdp_display_photometry.load(display_name)
            self.display_name = display_name
        else:
            self.display_photometry = display_photometry
            self.display_name = "unspecified"
        
        if display_geometry is None:
            self.display_geometry = vvdp_display_geometry.load(display_name)
        else:
            self.display_geometry = display_geometry

        self.pix_per_deg = self.display_geometry.get_ppd()
        self.imgaussfilt = utils.ImGaussFilt(0.5 * self.pix_per_deg, self.device)
        self.lpyr = None

    '''
    Predict image/video quality using FovVideoVDP.

    test_cont and reference_cont can be either numpy arrays or PyTorch tensors with images or video frames. 
        Depending on the display model (display_photometry), the pixel values should be either display encoded, or absolute linear.
        The two supported datatypes are float16 and uint8.
    dim_order - a string with the order of dimensions of test_cont and reference_cont. The individual characters denote
        B - batch
        C - colour channel
        F - frame
        H - height
        W - width
        Examples: "HW" - gray-scale image (column-major pixel order); "HWC" - colour image; "FCHW" - colour video
        The default order is "BCFHW". The processing can be a bit faster if data is provided in that order. 
    frame_padding - the metric requires at least 250ms of video for temporal processing. Because no previous frames exist in the
        first 250ms of video, the metric must pad those first frames. This options specifies the type of padding to use:
          'replicate' - replicate the first frame
          'circular'  - tile the video in the front, so that the last frame is used for frame 0.
          'pingpong'  - the video frames are mirrored so that frames -1, -2, ... correspond to frames 0, 1, ...
    '''
    def predict(self, test_cont, reference_cont, dim_order="BCFHW", frames_per_second=0):

        test_vs = video_source_array( test_cont, reference_cont, frames_per_second, dim_order=dim_order, display_photometry=self.display_photometry, color_space_name=self.color_space )

        return self.predict_video_source(test_vs)

    '''
    The same as `predict` but takes as input fvvdp_video_source_* object instead of Numpy/Pytorch arrays.
    '''
    def predict_video_source(self, vid_source):
        # We assume the pytorch default NCDHW layout

        vid_sz = vid_source.get_video_size() # H, W, F
        height, width, N_frames = vid_sz

        if self.lpyr is None or self.lpyr.W!=width or self.lpyr.H!=height:
            if self.contrast.startswith("weber"):
                self.lpyr = weber_contrast_pyr(width, height, self.pix_per_deg, self.device, contrast=self.contrast)
            elif self.contrast.startswith("log"):
                self.lpyr = log_contrast_pyr(width, height, self.pix_per_deg, self.device, contrast=self.contrast)
            else:
                raise RuntimeError( f"Unknown contrast {self.contrast}" )

            if self.do_heatmap:
                self.heatmap_pyr = lpyr_dec(width, height, self.pix_per_deg, self.device)

        #assert self.W == R_vid.shape[-1] and self.H == R_vid.shape[-2]
        #assert len(R_vid.shape)==5

        is_image = (N_frames==1)  # Can run faster on images

        if is_image:
            temp_ch = 1  # How many temporal channels
        else:
            temp_ch = 2
            self.F, omega_tmp = self.get_temporal_filters(vid_source.get_frames_per_second())
            self.filter_len = torch.numel(self.F[0])

        all_ch = 2+temp_ch

        if self.do_heatmap:
            dmap_channels = 1 if self.heatmap == "raw" else 3
            heatmap = torch.zeros([1,dmap_channels,N_frames,height,width], dtype=torch.float16, device=torch.device('cpu')) # Store heatmap in the CPU memory
        else:
            heatmap = None

        sw_buf = [None, None]
        Q_per_ch = None

        fl = self.filter_len

        if torch.cuda.is_available() and not is_image:
            # GPU utilization is better if we process many frames, but it requires more GPU memory

            # Determine how much memory we have
            total = torch.cuda.get_device_properties(self.device).total_memory
            allocated = torch.cuda.memory_allocated(self.device)
            mem_avail = total-allocated-1000000000  # Total available - 1G

            # Estimate how much we need for processing (may be inaccurate - to be improved)
            pix_cnt = width*height
            # sw_buf            
            mem_const = pix_cnt*4*3*2*(fl-1)
            # sw_buf + R + B_bands + L_bkg_pyr + (T_f + R_f) + S
            if self.debug: 
                self.mem_allocated_start = allocated
                self.mem_allocated_peak = 0

            #mem_per_frame = pix_cnt*4*3*2 + pix_cnt*4*all_ch*2 + int(pix_cnt*4*all_ch*2*1.33) + int(pix_cnt*4*2*1.33) + int(pix_cnt*4*2*1.33) + int(pix_cnt*4*1.33) 
            if self.use_checkpoints:           
                # More memory required when training. TODO: better way to detect when running with require_grad
                mem_per_frame = pix_cnt*2000   # Estimated memory required per frame
            else:
                mem_per_frame = pix_cnt*450   # Estimated memory required per frame

            max_frames = int((mem_avail-mem_const)/mem_per_frame) # how many frames can we fit into memory

            block_N_frames = max(1, min(max_frames,N_frames))  # Process so many frames in one pass 
            if self.debug: logging.debug( f"Processing {block_N_frames} frames in a batch." )
        else:
            block_N_frames = 1

        if self.contrast=="log":
            met_colorspace='logLMS_DKLd65'
        else:
            met_colorspace='DKLd65' # This metric uses DKL colourspaxce with d65 whitepoint

        for ff in range(0, N_frames, block_N_frames):
            cur_block_N_frames = min(block_N_frames,N_frames-ff) # How many frames in this block?

            if is_image:                
                R = torch.empty((1, 6, 1, height, width), device=self.device)
                R[:,0::2, :, :, :] = vid_source.get_test_frame(0, device=self.device, colorspace=met_colorspace)
                R[:,1::2, :, :, :] = vid_source.get_reference_frame(0, device=self.device, colorspace=met_colorspace)

            else: # This is video
                #if self.debug: print("Frame %d:\n----" % ff)

                if ff == 0: # First frame
                    sw_buf[0] = torch.empty((1,3,fl+block_N_frames-1,height,width), device=self.device, dtype=torch.float32) # TODO: switch to float16
                    sw_buf[1] = torch.empty((1,3,fl+block_N_frames-1,height,width), device=self.device, dtype=torch.float32)

                    if self.temp_padding == "replicate":
                        for fi in range(cur_block_N_frames):
                            ind = fl+fi-1
                            sw_buf[0][:,:,ind:ind+1,:,:] = vid_source.get_test_frame(ff+fi, device=self.device, colorspace=met_colorspace)
                            sw_buf[1][:,:,ind:ind+1,:,:] = vid_source.get_reference_frame(ff+fi, device=self.device, colorspace=met_colorspace)

                        ind = fl-1
                        sw_buf[0][:,:,0:-cur_block_N_frames,:,:] = sw_buf[0][:,:,ind:ind+1,:,:] # Replicate the first frame
                        sw_buf[1][:,:,0:-cur_block_N_frames,:,:] = sw_buf[1][:,:,ind:ind+1,:,:] # Replicate the first frame

                    # elif self.temp_padding == "circular":
                    #     sw_buf[0] = torch.zeros([1, 1, fl, height, width], device=self.device)
                    #     sw_buf[1] = torch.zeros([1, 1, fl, height, width], device=self.device)
                    #     for kk in range(fl):
                    #         fidx = (N_frames - 1 - fl + kk) % N_frames
                    #         sw_buf[0][:,:,kk,...] = vid_source.get_test_frame(fidx, device=self.device)
                    #         sw_buf[1][:,:,kk,...] = vid_source.get_reference_frame(fidx, device=self.device)
                    # elif self.temp_padding == "pingpong":
                    #     sw_buf[0] = torch.zeros([1, 1, fl, height, width], device=self.device)
                    #     sw_buf[1] = torch.zeros([1, 1, fl, height, width], device=self.device)

                    #     pingpong = list(range(0,N_frames)) + list(range(N_frames-2,0,-1))
                    #     indices = []
                    #     while(len(indices) < (fl-1)):
                    #         indices = indices + pingpong
                    #     indices = indices[-(fl-1):] + [0]

                    #     for kk in range(fl):
                    #         fidx = indices[kk]
                    #         sw_buf[0][:,:,kk,...] = vid_source.get_test_frame(fidx,device=self.device)
                    #         sw_buf[1][:,:,kk,...] = vid_source.get_reference_frame(fidx,device=self.device)
                    else:
                        raise RuntimeError( 'Unknown padding method "{}"'.format(self.temp_padding) )
                else:
                    # scroll the sliding window buffers
                    sw_buf[0][:,:,0:-cur_block_N_frames,:,:] = sw_buf[0][:,:,cur_block_N_frames:,:,:]
                    sw_buf[1][:,:,0:-cur_block_N_frames,:,:] = sw_buf[1][:,:,cur_block_N_frames:,:,:]

                    for fi in range(cur_block_N_frames):
                        ind=fl+fi-1
                        sw_buf[0][:,:,ind:ind+1,:,:] = vid_source.get_test_frame(ff+fi, device=self.device, colorspace=met_colorspace)
                        sw_buf[1][:,:,ind:ind+1,:,:] = vid_source.get_reference_frame(ff+fi, device=self.device, colorspace=met_colorspace)

                # Order: test-sustained-Y, ref-sustained-Y, test-rg, ref-rg, test-yv, ref-yv, test-transient-Y, ref-transient-Y
                # Images do not have the two last channels
                R = torch.zeros((1, 8, cur_block_N_frames, height, width), device=self.device)

                for cc in range(all_ch): # Iterate over chromatic and temporal channels
                    # 1D filter over time (over frames)
                    corr_filter = self.F[cc].flip(0).view([1,1,self.F[cc].shape[0],1,1]) 
                    sw_ch = 0 if cc==3 else cc # colour channel in the sliding window
                    for fi in range(cur_block_N_frames):
                        R[:,cc*2+0, fi, :, :] = (sw_buf[0][:, sw_ch, fi:(fl+fi), :, :] * corr_filter).sum(dim=-3,keepdim=True) # Test
                        R[:,cc*2+1, fi, :, :] = (sw_buf[1][:, sw_ch, fi:(fl+fi), :, :] * corr_filter).sum(dim=-3,keepdim=True) # Reference

            if self.use_checkpoints:
                # Used for training
                Q_per_ch_block = checkpoint.checkpoint(self.process_block_of_frames, R, vid_sz, temp_ch, self.lpyr, heatmap, use_reentrant=False)
            else:
                Q_per_ch_block = self.process_block_of_frames(R, vid_sz, temp_ch, self.lpyr, heatmap)

            if Q_per_ch is None:
                Q_per_ch = torch.zeros((Q_per_ch_block.shape[0], N_frames, Q_per_ch_block.shape[2]), device=self.device)
            
            Q_per_ch[:,ff:(ff+Q_per_ch_block.shape[1]),:] = Q_per_ch_block  

        rho_band = self.lpyr.get_freqs()
        Q_jod = self.do_pooling_and_jods(Q_per_ch, rho_band[0:-1])

        stats = {}
        stats['Q_per_ch'] = Q_per_ch.detach().cpu().numpy() # the quality per channel and per frame
        stats['rho_band'] = rho_band # Thespatial frequency per band
        stats['frames_per_second'] = vid_source.get_frames_per_second()
        stats['width'] = width
        stats['height'] = height
        stats['N_frames'] = N_frames

        if self.do_heatmap:            
            stats['heatmap'] = heatmap

        if self.debug and hasattr(self,"mem_allocated_peak"): 
            logging.debug( f"Allocated at start: {self.mem_allocated_start/1e9} GB" )
            logging.debug( f"Max allocated: {self.mem_allocated_peak/1e9} GB" )
            logging.debug( f"Resolution: {width}x{height} = {width*height/1e6} Mpixels" )
            pix_cnt = width*height
            # sw_buf            
            mem_const = pix_cnt*4*3*2*(fl-1)
            per_pixel = (self.mem_allocated_peak-self.mem_allocated_start-mem_const)/(pix_cnt*block_N_frames)
            logging.debug( f"Memory used per pixel: {per_pixel} B" )

        return (Q_jod.squeeze(), stats)

    # Perform pooling with per-band weights and map to JODs
    def do_pooling_and_jods(self, Q_per_ch, rho_band):
        # Q_per_ch[channel,frame,sp_band]

        # Weights for the two temporal channels
        no_channels = Q_per_ch.shape[0]
        no_frames = Q_per_ch.shape[1]
        no_bands = Q_per_ch.shape[2]
        if no_frames>1: # If video
            per_ch_w = self.ch_weights[0:no_channels].view(-1,1,1)
            #torch.stack( (torch.ones(1, device=self.device), torch.as_tensor(self.w_transient, device=self.device)[None] ), dim=1)[:,:,None]
        else: # If image
            per_ch_w = 1

        # Weights for the spatial bands
        per_sband_w = torch.ones( (1,1,no_bands), device=self.device)
        per_sband_w[0,0,-1] = self.baseband_weight

        #per_sband_w = torch.exp(interp1( self.quality_band_freq_log, self.quality_band_w_log, torch.log(torch.as_tensor(rho_band, device=self.device)) ))[:,None,None]

        Q_sc = self.lp_norm(Q_per_ch*per_ch_w*per_sband_w, self.beta_sch, dim=2, normalize=False)  # Sum across spatial channels
        Q_tc = self.lp_norm(Q_sc,     self.beta_tch, dim=0, normalize=False)  # Sum across temporal and chromatic channels
        Q    = self.lp_norm(Q_tc,     self.beta_t,   dim=1, normalize=True)   # Sum across frames
        Q = Q.squeeze()
            
        Q_JOD = 10. - self.jod_a * Q**self.jod_exp
        return Q_JOD

        # sign = lambda x: (1, -1)[x<0]
        # beta_jod = 10.0**self.log_jod_exp
        # Q_jod = sign(self.jod_a) * ((abs(self.jod_a)**(1.0/beta_jod))* Q)**beta_jod + 10.0 # This one can help with very large numbers
        # return Q_jod.squeeze()

    def process_block_of_frames(self, R, vid_sz, temp_ch, lpyr, heatmap):
        # R[channels,frames,width,height]
        #height, width, N_frames = vid_sz
        all_ch = 2+temp_ch

        # if self.contrast=="log":
        #     R = lms2006_to_dkld65( torch.log10(R.clip(min=1e-5)) )

        # Perform Laplacian pyramid decomposition
        B_bands, L_bkg_pyr = lpyr.decompose(R[0,...])

        if self.debug: assert len(B_bands) == lpyr.get_band_count()

        # if self.do_heatmap:
        #     Dmap_pyr_bands, Dmap_pyr_gbands = self.heatmap_pyr.decompose( torch.zeros([1,1,height,width], dtype=torch.float, device=self.device))

        # L_bkg_bb = [None for i in range(lpyr.get_band_count()-1)]

        rho_band = lpyr.get_freqs()
        rho_band[lpyr.get_band_count()-1] = 0.1 # Baseband

        Q_per_ch_block = None
        block_N_frames = R.shape[-3] 

        for bb in range(lpyr.get_band_count()):  # For each spatial frequency band

            is_baseband = (bb==(lpyr.get_band_count()-1))

            B_bb = lpyr.get_band(B_bands, bb) 
            T_f = B_bb[0::2,...] # Test
            R_f = B_bb[1::2,...] # Reference

            logL_bkg = lpyr.get_gband(L_bkg_pyr, bb)

            # Compute CSF
            rho = rho_band[bb] # Spatial frequency in cpd
            ch_height, ch_width = logL_bkg.shape[-2], logL_bkg.shape[-1]
            S = torch.empty((all_ch,block_N_frames,ch_height,ch_width), device=self.device)
            for cc in range(all_ch):
                tch = 0 if cc<3 else 1  # Sustained or transient
                cch = cc if cc<3 else 0 # Y, rg, yv
                tr = cc % logL_bkg.shape[-4] # Use L_bkg for the test or reference frame
                S[cc,:,:,:] = self.csf.sensitivity(rho, self.omega[tch], logL_bkg[...,tr,:,:,:], cch, self.csf_sigma) * 10.0**(self.sensitivity_correction/20.0)

            if is_baseband:
                D = (torch.abs(T_f-R_f) * S)
            else:
                D = self.apply_masking_model(T_f, R_f, S)

            # if self.do_heatmap:
            #     if cc == 0: self.heatmap_pyr.set_band(Dmap_pyr_bands, bb, D)
            #     else:       self.heatmap_pyr.set_band(Dmap_pyr_bands, bb, self.heatmap_pyr.get_band(Dmap_pyr_bands, bb) + w_temp_ch[cc] * D)

            if Q_per_ch_block is None:
                Q_per_ch_block = torch.empty((all_ch, block_N_frames, lpyr.get_band_count()), device=self.device)

            Q_per_ch_block[:,:,bb] = self.lp_norm(D, self.beta, dim=(-2,-1), normalize=True, keepdim=False) # Pool across all pixels (spatial pooling)

        # if self.do_heatmap:
        #     beta_jod = np.power(10.0, self.log_jod_exp)
        #     dmap = torch.pow(self.heatmap_pyr.reconstruct(Dmap_pyr_bands), beta_jod) * abs(self.jod_a)         
        #     if self.heatmap == "raw":
        #         heatmap[:,:,ff,...] = dmap.detach().type(torch.float16).cpu()
        #     else:
        #         ref_frame = R[:,0, :, :, :]
        #         heatmap[:,:,ff,...] = visualize_diff_map(dmap, context_image=ref_frame, colormap_type=self.heatmap).detach().type(torch.float16).cpu()
        
        return Q_per_ch_block

    def apply_masking_model(self, T, R, S):
        # T - test contrast tensor
        # R - reference contrast tensor
        # S - sensitivity
        T = T*S
        R = R*S
        M = self.phase_uncertainty( torch.min( torch.abs(T), torch.abs(R) ) )
        D = self.clamp_diffs( self.mask_func_perc_norm( torch.abs(T-R), M ) )

        if self.debug and hasattr(self,"mem_allocated_peak"): 
            allocated = torch.cuda.memory_allocated(self.device)
            self.mem_allocated_peak = max( self.mem_allocated_peak, allocated )

        return D

    def clamp_diffs(self,D):
        if self.dclamp_type == "hard":
            Dc = torch.clamp(D, max=(10**self.dclamp_par))
        elif self.dclamp_type == "soft":
            n = self.dclamp_par[0]
            off = self.dclamp_par[1]
            Dc = (D**n)/(off**n + D**n)
        else:
            raise RuntimeError( f"Unknown difference clamping type {self.dclamp_type}" )

        return Dc


    def phase_uncertainty(self, M):
        if self.pu_dilate != 0:
            M_pu = utils.imgaussfilt( M, self.pu_dilate ) * torch.pow(10.0, self.mask_c)
        else:
            M_pu = M * torch.pow(self.torch_scalar(10.0), self.torch_scalar(self.mask_c))
        return M_pu

    def mask_func_perc_norm(self, G, G_mask ):
        # Masking on perceptually normalized quantities (as in Daly's VDP)        
        p = self.mask_p
        if G_mask.shape[0]==3: # image
            q = torch.as_tensor( [self.mask_q_sust, self.mask_q_sust, self.mask_q_sust], device=self.device ).view(3,1,1,1)
        else: # video
            q = torch.as_tensor( [self.mask_q_sust, self.mask_q_sust, self.mask_q_sust, self.mask_q_trans], device=self.device ).view(4,1,1,1)
        R = torch.div(torch.pow(G,p), 1. + torch.pow(G_mask, q))
        return R


    def compute_local_contrast(self, T_f, R_f, lpyr, L_bkg_pyr, bb):
        if self.local_adapt=="simple_ref":
            L_bkg = lpyr.get_gband(L_bkg_pyr,bb)[1:2,:,:,:].clamp(min=0.01) # sustained, reference
            T = T_f / L_bkg  
            R = R_f / L_bkg
        else:
            raise RuntimeError( f"Error: local adaptation {self.local_adapt} not supported" )

        return L_bkg, T, R

    def weber2log(self, W):
        # Convert Weber contrast 
        #
        # W = (B-A)/A
        #
        # to log contrast
        #
        # G = log10( B/A );
        #
        return torch.log10(1.0 + W)

    def lp_norm(self, x, p, dim=0, normalize=True, keepdim=True):
        if dim is None:
            dim = 0

        if normalize:
            if isinstance(dim, tuple):
                N = 1.0
                for dd in dim:
                    N *= x.shape[dd]
            else:
                N = x.shape[dim]
        else:
            N = 1.0

        if isinstance( p, torch.Tensor ): 
            # p is a Tensor if it is being optimized. In that case, we need the formula for the norm
            return torch.pow( torch.sum(x ** (p), dim=dim, keepdim=keepdim)/float(N), 1/p) 
        else:
            return torch.norm(x, p, dim=dim, keepdim=keepdim) / (float(N) ** (1./p))

    # Return temporal filters
    # F[0] - Y sustained
    # F[1] - rg sustained
    # F[2] - yv sustained
    # F[3] - Y transient
    def get_temporal_filters(self, frames_per_s):

        N = int(math.ceil(0.250 * frames_per_s/2)*2)+1 # The length of the filter, always odd number
        N_omega = int(N/2)+1 # We need fewer freq coefficients as we use real FFT
        omega = torch.linspace( 0, frames_per_s/2, N_omega, device=self.device ).view(1,N_omega)

        R = torch.empty( (4, N_omega), device=self.device )
        # Sustained channels 
        R[0:3,:] = torch.exp( -omega ** self.beta_tf[0:3].view(3,1) / self.sigma_tf[0:3].view(3,1) )  # Freqency-space response
        # Transient channel
        omega_bands = torch.as_tensor( [0., 5.], device=self.device )
        R[3:4,:] = torch.exp( -(omega ** self.beta_tf[3] - omega_bands[1] ** self.beta_tf[3])**2  / self.sigma_tf[3] )  # Freqency-space response

        #r = torch.empty( (4, N), device=self.device )

        F = []
        for kk in range(4):
            # Must be executed once per each channel. For some reason, gives wrong results when run on the entire array
            r = torch.fft.fftshift( torch.real( torch.fft.irfft( R[kk,:], norm="backward", n=N ) ) ) 
            F.append( r )

        return F, omega_bands

    def torch_scalar(self, val, dtype=torch.float32):
        return torch.tensor(val, dtype=dtype, device=self.device) if not torch.is_tensor(val) else val.to(dtype)

    def short_name(self):
        return "cvvdp"

    def quality_unit(self):
        return "JOD"

    def get_info_string(self):
        if self.display_name.startswith('standard_'):
            #append this if are using one of the standard displays
            standard_str = self.display_name
        else:
            standard_str = f'custom-display: {self.display_name}'
        return f'"ColourVideoVDP v{self.version}, {self.pix_per_deg:.4g} [pix/deg], ' \
               f'Lpeak={self.display_photometry.get_peak_luminance():.5g}, ' \
               f'Lblack={self.display_photometry.get_black_level():.4g} [cd/m^2], ({standard_str})"'

    def write_features_to_json(self, stats, dest_fname):
        Q_per_ch = stats['Q_per_ch'] # quality per channel [cc,ff,bb]
        fmap = {}
        for key, value in stats.items():
            if not key in ["Q_per_ch", "heatmap"]:
                if isinstance(value, np.ndarray):
                    fmap[key] = value.tolist()
                else:
                    fmap[key] = value

        for cc in range(Q_per_ch.shape[0]): # for each temporal/chromatic channel
            for bb in range(Q_per_ch.shape[2]): # for each band
                fmap[f"t{cc}_b{bb}"] = Q_per_ch[cc,:,bb].tolist()

        with open(dest_fname, 'w', encoding='utf-8') as f:
            json.dump(fmap, f, ensure_ascii=False, indent=4)


