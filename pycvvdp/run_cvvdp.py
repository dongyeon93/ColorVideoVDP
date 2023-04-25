# Command-line interface for ColourVideoVDP. 

import os, sys
import os.path
import argparse
import logging
#from natsort import natsorted
import glob
import ffmpeg
import numpy as np
import torch
import imageio.v2 as imageio

import pycvvdp

#from pyfvvdp.fvvdp_display_model import fvvdp_display_photometry, fvvdp_display_geometry
# from pyfvvdp.visualize_diff_map import visualize_diff_map
#from pytorch_msssim import SSIM
import pycvvdp.utils as utils

def expand_wildcards(filestrs):
    if not isinstance(filestrs, list):
        return [ filestrs ]
    files = []
    for filestr in filestrs:
        if "*" in filestr:
            curlist = glob.glob(filestr)
            files = files + curlist
        else:
            files.append(filestr)
    return files

# Save a numpy array as a video
def np2vid(np_srgb, vidfile, fps, verbose=False):

    N, H, W, C = np_srgb.shape
    if C == 1:
        np_srgb = np.concatenate([np_srgb]*3, -1)
    process = (
        ffmpeg
            .input('pipe:', format='rawvideo', pix_fmt='rgb24', s='{}x{}'.format(W, H), r=fps)
            #.output(vidfile, format='mp4', **{ "v:q": "10" } )
            .output(vidfile, format='mp4', **{ "c:v": "mpeg4", "qscale:v": "3" } )  # mpeg4 codec is always bundled with ffmpeg so it should work
            .overwrite_output()
            .global_args( '-hide_banner')
            .global_args( '-loglevel', 'info' if verbose else 'quiet')
            .run_async(pipe_stdin=True)
    )
    for fid in range(N):
        process.stdin.write(
                (np_srgb[fid,...] * 255.0)
                .astype(np.uint8)
                .tobytes()
        )
    process.stdin.close()
    process.wait()

# Save a numpy array as an image
def np2img(np_srgb, imgfile):

    N, H, W, C = np_srgb.shape
    if C == 1:
        np_srgb = np.concatenate([np_srgb]*3, -1)

    if N>1:
        sys.exit("Expecting an image, found video")

    imageio.imwrite( imgfile, (np.clip(np_srgb,0.0,1.0)[0,...]*255.0).astype(np.uint8) )

# -----------------------------------
# Command-line Arguments
# -----------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Evaluate ColourVideoVDP on a set of videos")
    parser.add_argument("--test", type=str, nargs='+', required = False, help="list of test images/videos")
    parser.add_argument("--ref", type=str, nargs='+', required = False, help="list of reference images/videos")
    parser.add_argument("--device", type=str,  default='cuda:0', help="select which PyTorch device to use. Pick from ['cpu', 'mps', 'cuda:0', 'cuda:1', ...]")
    parser.add_argument("--heatmap", type=str, default="none", help="type of difference map (none, raw, threshold, supra-threshold).")
    parser.add_argument("--features", action='store_true', default=False, help="generate JSON files with extracted features. Useful for retraining the metric.")
    parser.add_argument("--output-dir", type=str, default=None, help="in which directory heatmaps and feature files should be stored (the default is the current directory)")
    parser.add_argument("--config-dir", type=str, default=None, help="A path to cvvdp configuration files: display_models.json, cvvdp_parameters.json and others.")
    parser.add_argument("--display", type=str, default="standard_4k", help="display name, e.g. 'HTC Vive', or ? to print the list of models.")
    parser.add_argument("--nframes", type=int, default=-1, help="the number of video frames you want to compare")
    parser.add_argument("--full-screen-resize", choices=['bilinear', 'bicubic', 'nearest', 'area'], default=None, help="Both test and reference videos will be resized to match the full resolution of the display. Currently works only with videos.")
    parser.add_argument("--metric", choices=['cvvdp', 'pu-psnr-rgb', 'pu-psnr-y'], nargs='+', default=['cvvdp'], help='Select which metric(s) to run')
    parser.add_argument("--temp-padding", choices=['replicate', 'circular', 'pingpong'], default='replicate', help='How to pad the video in the time domain (for the temporal filters). "replicate" - repeat the first frame. "pingpong" - mirror the first frames. "circular" - take the last frames.')
    parser.add_argument("--quiet", action='store_true', default=False, help="Do not print any information but the final JOD value. Warning message will be still printed.")
    parser.add_argument("--verbose", action='store_true', default=False, help="Print out extra information.")
    parser.add_argument("--ffmpeg-cc", action='store_true', default=False, help="Use ffmpeg for upsampling and colour conversion. Use custom pytorch code by default (faster and less memory).")
    args = parser.parse_args()
    return args

def main():
    args = parse_args()

    if args.quiet:
        log_level = logging.WARNING
    else:        
        log_level = logging.DEBUG if args.verbose else logging.INFO
        
    logging.basicConfig(format='[%(levelname)s] %(message)s', level=log_level)

    if not args.config_dir is None:
        pycvvdp.utils.config_files.set_config_dir(args.config_dir)

    if args.display == "?":
        pycvvdp.vvdp_display_photometry.list_displays()
        return

    if args.test is None or args.ref is None:
        logging.error( "Paths to both test and reference content needs to be specified.")
        return


    # Changed option to include MPS support for Macbooks
    # if args.gpu >= 0 and torch.cuda.is_available():
    #     device = torch.device('cuda:' + str(args.gpu))
    # else:
    #     device = torch.device('cpu')
    args.device = args.device.lower()
    if args.device.startswith('cuda') and torch.cuda.is_available():
        device = torch.device(args.device)
    elif args.device == 'mps':
        logging.warn('Some PyTorch operations are not yet supported for MPS. It is likely cvvdp will fail.')
        assert sys.platform == 'darwin', 'Device "mps" is only valid on a Mac.'
        device = torch.device(args.device)
    else:
        if args.device != 'cpu':
            logging.warn(f'The requested device ({args.device}) is not found, reverting to CPU. This may result in slow execution.')
        device = torch.device('cpu')

    logging.info("Running on device: " + str(device))

    # heatmap_types = {
    #     "threshold"   : {"scale" : 1.000, "colormap_type": "trichromatic"},
    #     "supra-threshold" : {"scale" : 0.333, "colormap_type": "dichromatic"},
    # }
    heatmap_types = ["raw", "threshold", "supra-threshold"]

    if args.heatmap == "none":
        args.heatmap = None

    if args.heatmap:
        if not args.heatmap in heatmap_types:
            logging.error( 'The recognized heatmap types are: "none", "raw", "threshold" and "supra-threshold"' )
            sys.exit()

        do_heatmap = True
    else:
        do_heatmap = False
        
    # Check for valid resizing methods
    #if args.gpu_decode:
        # Doc: https://pytorch.org/docs/stable/generated/torch.nn.functional.interpolate.html
        #valid_methods = ['nearest', 'bilinear', 'bicubic', 'area', 'nearest-exact']
    #else:
        # Doc: https://ffmpeg.org/ffmpeg-scaler.html
        #valid_methods = ['fast_bilinear', 'bilinear', 'bicubic', 'experimental', 'neighbor', 'area', 'bicublin', 'gauss', 'lanczos', 'spline']
    #if args.full_screen_size not in valid_methods:
    #    logging.error(f'The resizing method supplied is invalid. Please pick from {valid_methods}.')
    #    sys.exit()

    args.test = expand_wildcards(args.test)
    args.ref = expand_wildcards(args.ref)    

    N_test = len(args.test)
    N_ref = len(args.ref)

    if N_test==0:
        logging.error( "No test images/videos found." )
        sys.exit()

    if N_ref==0:
        logging.error( "No reference images/videos found." )
        sys.exit()

    if N_test != N_ref and N_test != 1 and N_ref != 1:
        logging.error( "Pass the same number of reference and test sources, or a single reference (to be used with all test sources), or a single test (to be used with all reference sources)." )
        sys.exit()

    metrics = []
    display_photometry = pycvvdp.vvdp_display_photometry.load(args.display)
    display_geometry = pycvvdp.vvdp_display_geometry.load(args.display)

    for mm in args.metric:
        if mm == 'cvvdp':
            fv = pycvvdp.cvvdp( display_name=args.display,
                                heatmap=args.heatmap, 
                                device=device,
                                temp_padding=args.temp_padding,
                                quiet=args.quiet )
            metrics.append( fv )
        elif mm == 'pu-psnr-rgb':
            if args.heatmap:
                logging.warning( f'Skipping heatmap as it is not supported by {mm}' )
            metrics.append( pycvvdp.pu_psnr_rgb2020(device=device) )
        elif mm == 'pu-psnr-y':
            if args.heatmap:
                logging.warning( f'Skipping heatmap as it is not supported by {mm}' )
            metrics.append( pycvvdp.pu_psnr_y(device=device) )
        else:
            raise RuntimeError( f"Unknown metric {mm}")

        info_str = metrics[-1].get_info_string()
        if not info_str is None:
            logging.info( 'When reporting metric results, please include the following information:' )
            logging.info( info_str )

    out_dir = "." if args.output_dir is None else args.output_dir
    os.makedirs(out_dir, exist_ok=True)

    for kk in range( max(N_test, N_ref) ): # For each test and reference pair
        test_file = args.test[min(kk,N_test-1)]
        ref_file = args.ref[min(kk,N_ref-1)]
        logging.info(f"Predicting the quality of '{test_file}' compared to '{ref_file}'")
        for mm in metrics:
            preload = False if args.temp_padding == 'replicate' else True
            with torch.no_grad():
                vs = pycvvdp.video_source_file( test_file, ref_file, 
                                                display_photometry=display_photometry, 
                                                full_screen_resize=args.full_screen_resize, 
                                                resize_resolution=display_geometry.resolution, 
                                                frames=args.nframes,
                                                preload=preload,
                                                ffmpeg_cc=args.ffmpeg_cc,
                                                verbose=args.verbose )
                Q_pred, stats = mm.predict_video_source(vs)
                if args.quiet:
                    print( "{Q:0.4f}".format(Q=Q_pred) )
                else:
                    units_str = f" [{mm.quality_unit()}]"
                    print( "{met_name}={Q:0.4f}{units}".format(met_name=mm.short_name(), Q=Q_pred, units=units_str) )

                base, ext = os.path.splitext(os.path.basename(test_file))            

                if args.features and not stats is None:
                    if mm == 'pu-psnr':
                        logging.warning( f'Skipping features as it is not supported by {mm}' )
                        break
                    dest_name = os.path.join(out_dir, base + "_fmap.json")
                    logging.info("Writing feature map '" + dest_name + "' ...")
                    mm.write_features_to_json(stats, dest_name)

                if do_heatmap and not stats is None:
                    # diff_type = heatmap_types[args.heatmap]
                    # heatmap = stats["heatmap"] * diff_type["scale"]
                    # diff_map_viz = visualize_diff_map(heatmap, context_image=ref_vid_luminance, colormap_type=diff_type["colormap_type"])
                    if stats["heatmap"].shape[2]>1: # if it is a video
                        dest_name = os.path.join(out_dir, base + "_heatmap.mp4")
                        logging.info("Writing heat map '" + dest_name + "' ...")
                        np2vid(torch.squeeze(stats["heatmap"].permute((2,3,4,1,0)), dim=4).cpu().numpy(), dest_name, vs.get_frames_per_second(), args.verbose)
                    else:
                        dest_name = os.path.join(out_dir, base + "_heatmap.png")
                        logging.info("Writing heat map '" + dest_name + "' ...")
                        np2img(torch.squeeze(stats["heatmap"].permute((2,3,4,1,0)), dim=4).cpu().numpy(), dest_name)
                        
                    del stats

    #     del test_vid
    #     torch.cuda.empty_cache()

if __name__ == '__main__':
    main()
