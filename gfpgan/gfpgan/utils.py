import cv2
import os
import torch
from basicsr.utils import img2tensor, tensor2img
from basicsr.utils.download_util import load_file_from_url
from facexlib.utils.face_restoration_helper import FaceRestoreHelper
from torchvision.transforms.functional import normalize

from gfpgan.archs.gfpgan_bilinear_arch import GFPGANBilinear
from gfpgan.archs.gfpganv1_arch import GFPGANv1
from gfpgan.archs.gfpganv1_clean_arch import GFPGANv1Clean
import time

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class GFPGANer():
    """Helper for restoration with GFPGAN.

    It will detect and crop faces, and then resize the faces to 512x512.
    GFPGAN is used to restored the resized faces.
    The background is upsampled with the bg_upsampler.
    Finally, the faces will be pasted back to the upsample background image.

    Args:
        model_path (str): The path to the GFPGAN model. It can be urls (will first download it automatically).
        upscale (float): The upscale of the final output. Default: 2.
        arch (str): The GFPGAN architecture. Option: clean | original. Default: clean.
        channel_multiplier (int): Channel multiplier for large networks of StyleGAN2. Default: 2.
        bg_upsampler (nn.Module): The upsampler for the background. Default: None.
    """

    def __init__(self, model_path, upscale=2, arch='clean', channel_multiplier=2, bg_upsampler=None, device=None):
        self.upscale = upscale
        self.bg_upsampler = bg_upsampler

        # initialize model
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu') if device is None else device
        # initialize the GFP-GAN
        if arch == 'clean':
            self.gfpgan = GFPGANv1Clean(
                out_size=512,
                num_style_feat=512,
                channel_multiplier=channel_multiplier,
                decoder_load_path=None,
                fix_decoder=False,
                num_mlp=8,
                input_is_latent=True,
                different_w=True,
                narrow=1,
                sft_half=True)
        elif arch == 'bilinear':
            self.gfpgan = GFPGANBilinear(
                out_size=512,
                num_style_feat=512,
                channel_multiplier=channel_multiplier,
                decoder_load_path=None,
                fix_decoder=False,
                num_mlp=8,
                input_is_latent=True,
                different_w=True,
                narrow=1,
                sft_half=True)
        elif arch == 'original':
            self.gfpgan = GFPGANv1(
                out_size=512,
                num_style_feat=512,
                channel_multiplier=channel_multiplier,
                decoder_load_path=None,
                fix_decoder=True,
                num_mlp=8,
                input_is_latent=True,
                different_w=True,
                narrow=1,
                sft_half=True)
        # initialize face helper
        self.face_helper = FaceRestoreHelper(
            upscale,
            face_size=512,
            crop_ratio=(1, 1),
            det_model='retinaface_resnet50',
            save_ext='png',
            use_parse=True,
            device=self.device)

        if model_path.startswith('https://'):
            model_path = load_file_from_url(
                url=model_path, model_dir=os.path.join(ROOT_DIR, 'gfpgan/weights'), progress=True, file_name=None)
        loadnet = torch.load(model_path)
        if 'params_ema' in loadnet:
            keyname = 'params_ema'
        else:
            keyname = 'params'
        self.gfpgan.load_state_dict(loadnet[keyname], strict=True)
        self.gfpgan.eval()
        self.gfpgan = self.gfpgan.to(self.device)

    @torch.no_grad()
    def enhance(self, img, has_aligned=False, only_center_face=False, paste_back=True):
        self.face_helper.clean_all()
        # d = 0
        # g = 0
        # u = 0
        if has_aligned:  # the inputs are already aligned
            img = cv2.resize(img, (512, 512))
            self.face_helper.cropped_faces = [img]
        else:
            # s = time.time()
            self.face_helper.read_image(img)
            # get face landmarks for each face
            s = time.time()
            self.face_helper.get_face_landmarks_5(only_center_face=only_center_face, eye_dist_threshold=5)
            e = time.time()
            print(f"landmark: {e-s}")
            # eye_dist_threshold=5: skip faces whose eye distance is smaller than 5 pixels
            # TODO: even with eye_dist_threshold, it will still introduce wrong detections and restorations.
            # align and warp each face
            s = time.time()
            self.face_helper.align_warp_face()
            e = time.time()
            print(f"align: {e-s}")
            # e = time.time()
            # print(f"detecting time: {e-s}")
            # d = e-s
        # face restoration
        for cropped_face in self.face_helper.cropped_faces:
            # prepare data
            cropped_face_t = img2tensor(cropped_face / 255., bgr2rgb=True, float32=True)
            normalize(cropped_face_t, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True)
            cropped_face_t = cropped_face_t.unsqueeze(0).to(self.device)

            try:
                # s = time.time()
                output = self.gfpgan(cropped_face_t, return_rgb=False)[0]
                # convert to image
                restored_face = tensor2img(output.squeeze(0), rgb2bgr=True, min_max=(-1, 1))
                # e = time.time()
                # print(f"gfpgan time: {e-s}")
                # g = e-s
            except RuntimeError as error:
                print(f'\tFailed inference for GFPGAN: {error}.')
                restored_face = cropped_face

            restored_face = restored_face.astype('uint8')
            self.face_helper.add_restored_face(restored_face)
        # s = time.time()
        # bg_img = self.bg_upsampler.enhance(restored_face, outscale=self.upscale)[0]
        # self.face_helper.get_inverse_affine(None)
        # restored_img = self.face_helper.paste_faces_to_input_image(upsample_img=bg_img)
        # e = time.time()
        # u = e - s
        if not has_aligned and paste_back:
            # upsample the background
            s = time.time()
            if self.bg_upsampler is not None:
                # Now only support RealESRGAN for upsampling background
                bg_img = self.bg_upsampler.enhance(img, outscale=self.upscale)[0]
            else:
                bg_img = None

            self.face_helper.get_inverse_affine(None)
            # paste each restored face to the input image
            restored_img = self.face_helper.paste_faces_to_input_image(upsample_img=bg_img)
            e = time.time()
            print(f"upsample time: {e-s}")
            # u = e - s
            return self.face_helper.cropped_faces, self.face_helper.restored_faces, restored_img
        else:
            return self.face_helper.cropped_faces, self.face_helper.restored_faces, None