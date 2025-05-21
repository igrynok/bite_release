
# [barc2] python scripts/train.py --workers 12 --checkpoint project22_no3dcgloss_smaldogsilvia_v0 --loss-weight-path barc_loss_weights_no3dcgloss.json --config barc_cfg_train.yaml start --model-file-hg hg_ksp_fromnewanipose_stanext_v0/checkpoint.pth.tar --model-file-3d barc_normflow_pret/checkpoint.pth.tar
# [barc3] python scripts/train.py --workers 12 --checkpoint project22_no3dcgloss_smaldognadine_v0 --loss-weight-path barc_loss_weights_no3dcgloss.json --config barc_cfg_train.yaml start --model-file-hg hg_ksp_fromnewanipose_stanext_v0/checkpoint.pth.tar --model-file-3d barc_normflow_pret/checkpoint.pth.tar

# python scripts/train_withref.py --workers 12 --checkpoint project22_no3dcgloss_smaldognadine_v4_ref_v0 --loss-weight-path barc_loss_weights_no3dcgloss.json --config refinement_cfg_train.yaml continue --model-file-complete project22_no3dcgloss_smaldognadine_v4/checkpoint.pth.tar --new-optimizer 1
# python scripts/train_withref.py --workers 12 --checkpoint project22_no3dcgloss_smaldognadine_v4_refadd_v0 --loss-weight-path barc_loss_weights_no3dcgloss.json --config refinement_cfg_train.yaml continue --model-file-complete project22_no3dcgloss_smaldognadine_v4/checkpoint.pth.tar --new-optimizer 1



print('start ...')
import numpy as np
import random
import torch
import argparse
import os
import json
import torch
import torch.backends.cudnn
from torch.nn import DataParallel
from torch.optim.rmsprop import RMSprop
from torch.utils.data import DataLoader
from tqdm import trange, tqdm
from collections import OrderedDict
from itertools import chain
import shutil 

# set random seeds (we have never changed those and there is probably one missing)
torch.manual_seed(52)
np.random.seed(435)
random.seed(643)

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../', 'src'))
# from combined_model.train_main_image_to_3d_withbreedrel import do_training_epoch, do_validation_epoch
from combined_model.train_main_image_to_3d_wbr_withref import do_training_epoch, do_validation_epoch
# from combined_model.model_shape_v7 import ModelImageTo3d_withshape_withproj 
# from combined_model.model_shape_v7_withref import ModelImageTo3d_withshape_withproj 
from combined_model.model_shape_v7_withref_withgraphcnn import ModelImageTo3d_withshape_withproj 

from combined_model.loss_image_to_3d_withbreedrel import Loss
from combined_model.loss_image_to_3d_refinement import LossRef
from stacked_hourglass.utils.misc import save_checkpoint, adjust_learning_rate
from stacked_hourglass.datasets.samplers.custom_pair_samplers import CustomPairBatchSampler
from stacked_hourglass.datasets.samplers.custom_gc_sampler import CustomGCSampler
from stacked_hourglass.datasets.samplers.custom_gc_sampler_noclasses import CustomGCSamplerNoCLass
from configs.barc_cfg_defaults import get_cfg_defaults, update_cfg_global_with_yaml, get_cfg_global_updated



class PrintLog():
    def __init__(self, out_file):
        self.out_file = out_file
        # self._print_to_file('------------------------------------------------------')
    def clean_file(self):
        # this function deletes all content of that file
        with open(self.out_file,'w') as file:
            pass
    def _print_to_file(self, *args, **kwargs):
        with open(self.out_file,'a') as file:
            print(*args, **kwargs, file=file)
    def print(self, *args, **kwargs):
        print(*args, **kwargs)
        self._print_to_file(*args, **kwargs)
    def print_log_only(self, *args, **kwargs):
        self._print_to_file(*args, **kwargs)


def main(args):

    # load all configs and weights 
    #   step 1: load default configs
    #   step 2: load updates from .yaml file
    #   step 3: load training weights
    path_config = os.path.join(get_cfg_defaults().barc_dir, 'src', 'configs', args.config)
    update_cfg_global_with_yaml(path_config)
    cfg = get_cfg_global_updated()
    with open(os.path.join(os.path.dirname(__file__), '../', 'src', 'configs', args.loss_weight_path), 'r') as f:
        weight_dict = json.load(f)
    with open(os.path.join(os.path.dirname(__file__), '../', 'src', 'configs', args.loss_weight_ref_path), 'r') as f:
        weight_dict_ref = json.load(f)
    # Select the hardware device to use for training.
    if torch.cuda.is_available() and cfg.device=='cuda':
        device = torch.device('cuda', torch.cuda.current_device())
        torch.backends.cudnn.benchmark = True
    else:
        device = torch.device('cpu')

    # import data loader
    if cfg.data.DATASET == 'stanext24_easy':
        from stacked_hourglass.datasets.stanext24_easy import StanExtEasy as StanExt 
    elif cfg.data.DATASET == 'stanext24':
        from stacked_hourglass.datasets.stanext24 import StanExt 
    elif cfg.data.DATASET == 'stanext24_withgc':
        from stacked_hourglass.datasets.stanext24_withgc import StanExtGC as StanExt ###################
    elif cfg.data.DATASET == 'stanext24_withgc_big':
        from stacked_hourglass.datasets.stanext24_withgc_v2 import StanExtGC as StanExt 
    elif cfg.data.DATASET == 'stanext24_withgc_cs0':
        from stacked_hourglass.datasets.stanext24_withgc_v2 import StanExtGC as StanExt 
        #   -> same dataset as in stanext24_withgc_big, but different training sampler
    elif cfg.data.DATASET == 'stanext24_withgc_csaddnonflat':
        from stacked_hourglass.datasets.stanext24_withgc_v2 import StanExtGC as StanExt 
    elif cfg.data.DATASET == 'stanext24_withgc_csaddnonflatmorestanding':
        from stacked_hourglass.datasets.stanext24_withgc_v2 import StanExtGC as StanExt 
    elif cfg.data.DATASET == 'stanext24_withgc_noclasses':
        from stacked_hourglass.datasets.stanext24_withgc_v2 import StanExtGC as StanExt 
    else:
        raise NotImplementedError

    # Disable gradient calculations by default.
    torch.set_grad_enabled(False)

    # create checkpoint dir
    path_checkpoint = os.path.join(cfg.paths.ROOT_CHECKPOINT_PATH, args.checkpoint)
    os.makedirs(path_checkpoint, exist_ok=True)

    # copy the python train file
    in_train_file = os.path.abspath(__file__)
    out_train_file_dir = os.path.join(path_checkpoint)
    shutil.copy2(in_train_file, out_train_file_dir)
    shutil.copy2(os.path.join(os.path.dirname(__file__), '../', 'src', 'configs', args.loss_weight_ref_path), path_checkpoint) 

    # create printlog
    pl = PrintLog(out_file=path_checkpoint + '/partial_log.txt')
    pl.print('------------------------------------------------------')

    # print some information
    pl.print('dataset: ' + cfg.data.DATASET)
    pl.print('structure_pose_net: ' + cfg.params.STRUCTURE_POSE_NET)
    pl.print('refinement network type: ' + cfg.params.REF_NET_TYPE)
    pl.print('refinement network detach shape: ' + str(cfg.params.REF_DETACH_SHAPE))
    pl.print('graphcnn_type: ' + cfg.params.GRAPHCNN_TYPE)
    pl.print('isflat_type: ' + cfg.params.ISFLAT_TYPE)
    pl.print('shaperef_type: ' + cfg.params.SHAPEREF_TYPE)
    pl.print('smal_model_type: ' + cfg.smal.SMAL_MODEL_TYPE)
    pl.print('train_parts: ' +  cfg.optim.TRAIN_PARTS)

    # load model
    if weight_dict['partseg'] > 0:
        render_partseg = True
    else:
        render_partseg = False
    model = ModelImageTo3d_withshape_withproj(
        opts=cfg, # Pass the full config object
        smal_model_type=cfg.smal.SMAL_MODEL_TYPE, smal_keyp_conf=cfg.smal.SMAL_KEYP_CONF, \
        num_stage_comb=cfg.params.NUM_STAGE_COMB, num_stage_heads=cfg.params.NUM_STAGE_HEADS, \
        num_stage_heads_pose=cfg.params.NUM_STAGE_HEADS_POSE, trans_sep=cfg.params.TRANS_SEP, \
        arch=cfg.params.ARCH, n_joints=cfg.params.N_JOINTS, n_classes=cfg.params.N_CLASSES, \
        n_keyp=cfg.params.N_KEYP, n_bones=cfg.params.N_BONES, n_betas=cfg.params.N_BETAS, n_betas_limbs=cfg.params.N_BETAS_LIMBS, \
        n_breeds=cfg.params.N_BREEDS, n_z=cfg.params.N_Z, image_size=cfg.params.IMG_SIZE, \
        silh_no_tail=cfg.params.SILH_NO_TAIL, thr_keyp_sc=cfg.params.KP_THRESHOLD, add_z_to_3d_input=cfg.params.ADD_Z_TO_3D_INPUT,
        n_segbps=cfg.params.N_SEGBPS, add_segbps_to_3d_input=cfg.params.ADD_SEGBPS_TO_3D_INPUT, add_partseg=cfg.params.ADD_PARTSEG, n_partseg=cfg.params.N_PARTSEG, \
        fix_flength=cfg.params.FIX_FLENGTH, render_partseg=render_partseg, structure_z_to_betas=cfg.params.STRUCTURE_Z_TO_B, \
        structure_pose_net=cfg.params.STRUCTURE_POSE_NET, nf_version=cfg.params.NF_VERSION, ref_net_type=cfg.params.REF_NET_TYPE, \
        ref_detach_shape=cfg.params.REF_DETACH_SHAPE, graphcnn_type=cfg.params.GRAPHCNN_TYPE, isflat_type=cfg.params.ISFLAT_TYPE, shaperef_type=cfg.params.SHAPEREF_TYPE)
    model = model.to(device)

    # define parameters that should be optimized
    if cfg.optim.TRAIN_PARTS == 'all_with_shapedirs':       # do not use this option!
        params = chain(model.breed_model.parameters(), \
                    model.model_3d.parameters(), \
                    model.model_learnable_shapedirs.parameters())
    elif cfg.optim.TRAIN_PARTS == 'all_without_shapedirs':
        params = chain(model.breed_model.parameters(), \
                        model.model_3d.parameters())
    elif cfg.optim.TRAIN_PARTS == 'model3donly_noshape_noshapedirs':
        params = chain(model.model_3d.parameters())
    elif cfg.optim.TRAIN_PARTS == 'all_noresnetclass_without_shapedirs':
        params = chain(model.breed_model.linear_breeds.parameters(), \
                        model.model_3d.parameters()) 
    elif cfg.optim.TRAIN_PARTS == 'breed_model':
        params = chain(model.breed_model.parameters())
    elif cfg.optim.TRAIN_PARTS == 'flength_trans_betas_only':
        params = chain(model.model_3d.output_info_linear_models[1].parameters(), \
        model.model_3d.output_info_linear_models[2].parameters(), \
        model.model_3d.output_info_linear_models[3].parameters(), \
        model.breed_model.linear_betas.parameters())
    elif cfg.optim.TRAIN_PARTS == 'all_without_shapedirs_with_refinement':
        params = chain(model.breed_model.parameters(), \
                        model.model_3d.parameters(), \
                        model.refinement_model.parameters())
    elif cfg.optim.TRAIN_PARTS == 'refinement_model':
        params = chain(model.refinement_model.parameters())
    elif cfg.optim.TRAIN_PARTS == 'refinement_model_and_shape':
        params = chain(model.refinement_model.parameters(), \
                        model.breed_model.parameters())
    else:
        raise NotImplementedError
                
    # create optimizer
    optimizer = RMSprop(params, lr=cfg.optim.LR, momentum=cfg.optim.MOMENTUM, weight_decay=cfg.optim.WEIGHT_DECAY)
    start_epoch = 0
    best_acc = 0

    # load pretrained model or parts of the model
    if args.command == "start":
        path_model_file_hg = os.path.join(cfg.paths.ROOT_CHECKPOINT_PATH, args.model_file_hg)
        path_model_file_shape = os.path.join(cfg.paths.ROOT_CHECKPOINT_PATH, args.model_file_shape)
        path_model_file_3d = os.path.join(cfg.paths.ROOT_CHECKPOINT_PATH, args.model_file_3d)
        # (1) load pretrained shape model
        #   -> usually we do not work with a pretrained model here
        if os.path.isfile(path_model_file_shape): 
            pl.print('Loading model weights for shape network from a separate file: {}'.format(path_model_file_shape))
            checkpoint_shape = torch.load(path_model_file_shape)
            state_dict_shape = checkpoint_shape['state_dict']
            # model.load_state_dict(state_dict_complete, strict=False)   
            # --- Problem: there is the last layer which predicts betas and we might change the numbers of betas 
            # NEW: allow to load the model even if the number of betas is different
            model_dict = model.state_dict()
            # i) filter out unnecessary keys and remove weights for layers that have changed shapes (smal.shapedirs, resnet18.fc.weight, ...)
            state_dict_shape_new = OrderedDict()
            for k, v in state_dict_shape.items():
                if k in model_dict:
                    if v.shape==model_dict[k].shape:
                        state_dict_shape_new[k] = v
                    else:
                        state_dict_shape_new[k] = model_dict[k]
            # ii) overwrite entries in the existing state dict
            model_dict.update(state_dict_shape_new) 
            # iii) load the new state dict
            model.load_state_dict(model_dict)
        # (2) load pretrained 3d network
        #    -> we recommend to load a pretrained model
        if os.path.isfile(path_model_file_3d): 
            assert os.path.isfile(path_model_file_3d)
            pl.print('Loading model weights (2d-to-3d) from file: {}'.format(path_model_file_3d))
            checkpoint_3d = torch.load(path_model_file_3d)
            state_dict_3d = checkpoint_3d['state_dict']
            model.load_state_dict(state_dict_3d, strict=False) 
        else:
            pl.print('no model (2d-to-3d) loaded')
        # (3) initialize weights for stacked hourglass
        #   -> the stacked hourglass needs to be pretrained
        assert os.path.isfile(path_model_file_hg)
        pl.print('Loading model weights (stacked hourglass) from file: {}'.format(path_model_file_hg))
        checkpoint = torch.load(path_model_file_hg)
        state_dict = checkpoint['state_dict']
        if sorted(state_dict.keys())[0].startswith('module.'):
            new_state_dict = OrderedDict()
            for k, v in state_dict.items():
                name = k[7:] # remove 'module.' of dataparallel
                new_state_dict[name]=v
            state_dict = new_state_dict
        model.stacked_hourglass.load_state_dict(state_dict)
    elif args.command == "continue":
        path_model_file_complete = os.path.join(cfg.paths.ROOT_CHECKPOINT_PATH, args.model_file_complete) 
        pl.print('Loading complete model weights from file: {}'.format(path_model_file_complete))
        checkpoint = torch.load(path_model_file_complete)
        model.load_state_dict(checkpoint['state_dict'], strict=False)
        if args.new_optimizer == 0:
            pl.print('load optimizer state')
            start_epoch = checkpoint['epoch']
            best_acc = checkpoint['best_acc']
            optimizer.load_state_dict(checkpoint['optimizer'])
        else:
            pl.print('do not load optimizer state')

            

    # load loss module
    loss_module = Loss(smal_model_type=cfg.smal.SMAL_MODEL_TYPE, data_info=StanExt.DATA_INFO, nf_version=cfg.params.NF_VERSION).to(device)    
    loss_module_ref = LossRef(smal_model_type=cfg.smal.SMAL_MODEL_TYPE, data_info=StanExt.DATA_INFO, nf_version=cfg.params.NF_VERSION).to(device)    

    # print weight_dict
    pl.print("weight_dict: ")
    pl.print(weight_dict)
    pl.print("weight_dict_ref: ")
    pl.print(weight_dict_ref)


    if cfg.data.DATASET in ['stanext24_withgc', 'stanext24_withgc_big']:
        # NEW for ground contact
        pl.print("WARNING: we use a data sampler with ground contact that is not fully ready!")
        pl.print('use a very standard data loader that is not suitable for breed losses!')
        dataset_mode='complete_with_gc'
        train_dataset = StanExt(image_path=None, is_train=True, dataset_mode=dataset_mode, V12=cfg.data.V12, val_opt=cfg.data.VAL_OPT, shorten_dataset_to=cfg.data.SHORTEN_VAL_DATASET_TO)
        train_loader = DataLoader(
            train_dataset,
            batch_size=cfg.optim.BATCH_SIZE, shuffle=True,
            num_workers=args.workers, pin_memory=True, 
            drop_last=True)

        val_dataset = StanExt(image_path=None, is_train=False, dataset_mode=dataset_mode, V12=cfg.data.V12, val_opt=cfg.data.VAL_OPT)
        val_loader = DataLoader(
            val_dataset,
            batch_size=cfg.optim.BATCH_SIZE, shuffle=False,
            num_workers=args.workers, pin_memory=True,
            drop_last=True)         # drop last, need to check that!!
    elif cfg.data.DATASET in ['stanext24_withgc_cs0', 'stanext24_withgc_csaddnonflat', 'stanext24_withgc_csaddnonflatmorestanding']:      # cs0: custom sampler 0
        dataset_mode='complete_with_gc'
        if cfg.data.DATASET  == 'stanext24_withgc_cs0': 
            add_nonflat = False
            more_standing = False
            assert cfg.optim.BATCH_SIZE == 12
            pl.print('use CustomGCSampler without nonflat images')
        elif cfg.data.DATASET  == 'stanext24_withgc_csaddnonflat': 
            add_nonflat = True
            more_standing = False
            pl.print('use CustomGCSampler (with 12 flat and with 2 nonflat images)')
            assert cfg.optim.BATCH_SIZE == 14
        else:   # stanext24_withgc_csaddnonflatmorestanding
            add_nonflat = True
            more_standing = True
            pl.print('use CustomGCSampler (with 12 flat and with 2 nonflat images, more standing poses)')
            assert cfg.optim.BATCH_SIZE == 14
        train_dataset = StanExt(opts=cfg, image_path=None, is_train=True, dataset_mode=dataset_mode, V12=cfg.data.V12, val_opt=cfg.data.VAL_OPT, add_nonflat=add_nonflat)
        data_sampler_info_gc = train_dataset.get_data_sampler_info_gc()
        batch_sampler = CustomGCSampler
        train_custom_batch_sampler = batch_sampler(data_sampler_info_gc=data_sampler_info_gc, batch_size=cfg.optim.BATCH_SIZE, add_nonflat=add_nonflat, more_standing=more_standing)        
        train_loader = DataLoader(
            train_dataset,
            batch_sampler=train_custom_batch_sampler,
            num_workers=args.workers, pin_memory=True)
        val_dataset = StanExt(opts=cfg, image_path=None, is_train=False, dataset_mode=dataset_mode, V12=cfg.data.V12, val_opt=cfg.data.VAL_OPT)
        val_loader = DataLoader(
            val_dataset,
            batch_size=cfg.optim.BATCH_SIZE, shuffle=False,
            num_workers=args.workers, pin_memory=True,
            drop_last=True)         # drop last, need to check that!!
    elif cfg.data.DATASET == 'stanext24_withgc_noclasses':
        dataset_mode='complete_with_gc'
        add_nonflat = True
        assert cfg.optim.BATCH_SIZE == 14
        pl.print('use CustomGCSamplerNoCLass (with nonflat images)')
        train_dataset = StanExt(image_path=None, is_train=True, dataset_mode=dataset_mode, V12=cfg.data.V12, val_opt=cfg.data.VAL_OPT, add_nonflat=add_nonflat)
        data_sampler_info_gc = train_dataset.get_data_sampler_info_gc()
        batch_sampler = CustomGCSamplerNoCLass
        train_custom_batch_sampler = batch_sampler(data_sampler_info_gc=data_sampler_info_gc, batch_size=cfg.optim.BATCH_SIZE, add_nonflat=add_nonflat)        
        train_loader = DataLoader(
            train_dataset,
            batch_sampler=train_custom_batch_sampler,
            num_workers=args.workers, pin_memory=True)
        val_dataset = StanExt(image_path=None, is_train=False, dataset_mode=dataset_mode, V12=cfg.data.V12, val_opt=cfg.data.VAL_OPT)
        val_loader = DataLoader(
            val_dataset,
            batch_size=cfg.optim.BATCH_SIZE, shuffle=False,
            num_workers=args.workers, pin_memory=True,
            drop_last=True)         # drop last, need to check that!!


    else:

        dataset_mode='complete'

        # load data sampler
        if ('0' in weight_dict['breed_options']) or ('1' in weight_dict['breed_options']) or ('2' in weight_dict['breed_options']):
            # remark: you will not need this data loader, it was only relevant for some of our experiments related to clades
            batch_sampler = CustomBatchSampler
            pl.print('use CustomBatchSampler')
        else:
            # this sampler will always load two dogs of the same breed right after each other  
            batch_sampler = CustomPairBatchSampler
            pl.print('use CustomPairBatchSampler')

        # load dataset (train and {test or val})
        train_dataset = StanExt(image_path=None, is_train=True, dataset_mode=dataset_mode, V12=cfg.data.V12, val_opt=cfg.data.VAL_OPT)
        data_sampler_info = train_dataset.get_data_sampler_info()
        train_custom_batch_sampler = batch_sampler(data_sampler_info=data_sampler_info, batch_size=cfg.optim.BATCH_SIZE)
        train_loader = DataLoader(
            train_dataset,
            batch_sampler=train_custom_batch_sampler,
            num_workers=args.workers, pin_memory=True)

        if cfg.data.VAL_METRICS == 'no_loss':
            # this is the option that we choose normally
            # here we load val/test images using a standard sampler 
            # using a standard sampler at test time is better, but it prevents us from evaluating all the loss functions used at training time
            #   -> with this option here we calculate iou and pck for the val/test batches 
            val_dataset = StanExt(image_path=None, is_train=False, dataset_mode=dataset_mode, V12=cfg.data.V12, val_opt=cfg.data.VAL_OPT, shorten_dataset_to=cfg.data.SHORTEN_VAL_DATASET_TO)
            val_loader = DataLoader(
                val_dataset,
                batch_size=cfg.optim.BATCH_SIZE, shuffle=False,
                num_workers=args.workers, pin_memory=True)
        else:
            # this is an option we might choose for debugging purposes
            # here we load val/test images using our custom sampler for pairs of dogs of the same breed
            val_dataset = StanExt(image_path=None, is_train=False, dataset_mode=dataset_mode, V12=cfg.data.V12, val_opt=cfg.data.VAL_OPT)
            data_sampler_info = val_dataset.get_data_sampler_info()
            val_custom_batch_sampler = batch_sampler(data_sampler_info=data_sampler_info, batch_size=cfg.optim.BATCH_SIZE, drop_last=True)
            val_loader = DataLoader(
                val_dataset,
                batch_sampler=val_custom_batch_sampler,
                num_workers=args.workers, pin_memory=True)   
 
    # train and eval
    lr = cfg.optim.LR
    pl.print('initial learning rate: ' + str(lr))
    for epoch in trange(0, cfg.optim.EPOCHS, desc='Overall', ascii=True):
        lr = adjust_learning_rate(optimizer, epoch, lr, cfg.optim.SCHEDULE, cfg.optim.GAMMA)
        if epoch >= start_epoch:
            # train for one epoch
            train_string, train_acc = do_training_epoch(train_loader, model, loss_module, loss_module_ref, device, 
                                                    StanExt.DATA_INFO,
                                                    optimizer,
                                                    weight_dict=weight_dict,
                                                    weight_dict_ref=weight_dict_ref,
                                                    acc_joints=StanExt.ACC_JOINTS)
            # evaluate on validation set
            save_imgs_path = None   
            valid_string, valid_acc = do_validation_epoch(val_loader, model, loss_module, loss_module_ref, device,
                                                                    StanExt.DATA_INFO,
                                                                    weight_dict=weight_dict,
                                                                    weight_dict_ref=weight_dict_ref,
                                                                    acc_joints=StanExt.ACC_JOINTS,
                                                                    metrics=cfg.data.VAL_METRICS, 
                                                                    save_imgs_path=save_imgs_path)
            predictions = np.zeros((1,1))
            train_loss = - train_acc
            valid_loss = - valid_acc        
            # print metrics
            tqdm.write(f'[{epoch + 1:3d}/{cfg.optim.EPOCHS:3d}] lr={lr:0.2e}' + '   | TRAIN: ' +  train_string + '   | VAL: ' + valid_string)
            pl.print_log_only(f'[{epoch + 1:3d}/{cfg.optim.EPOCHS:3d}] lr={lr:0.2e}' + '   | TRAIN: ' +  train_string + '   | VAL: ' + valid_string)

            # remember best acc (acc is actually iou) and save checkpoint
            is_best = valid_acc > best_acc
            best_acc = max(valid_acc, best_acc)
            save_checkpoint({
                'epoch': epoch + 1,
                'arch': cfg.params.ARCH,
                'state_dict': model.state_dict(),
                'best_acc': best_acc,
                'optimizer' : optimizer.state_dict(),
            }, predictions, is_best, checkpoint=path_checkpoint, snapshot=args.snapshot)
    

if __name__ == '__main__':

    # use as follows:
    # python scripts/train_image_to_3d_withshape_withbreedrel.py --workers 12  --checkpoint=barc_new_v2 start --model-file-hg dogs_hg8_ksp_24_sev12_v3/model_best.pth.tar --model-file-3d Normflow_CVPR_set8_v3k2_v1/checkpoint.pth.tar

    parser = argparse.ArgumentParser(description='Train a image-to-3d model.')

    # arguments that we have no matter if we start a new training run or if we load the full network where training is somewhere in the middle
    parser.add_argument('-c', '--checkpoint', default='checkpoint', type=str, metavar='PATH',
                        help='path to save checkpoint (default: checkpoint)')
    parser.add_argument('-cg', '--config', default='barc_cfg_train.yaml', type=str, metavar='PATH',
                        help='name of config file (default: barc_cfg_train.yaml within src/configs folder)')
    parser.add_argument('-lw', '--loss-weight-path', default='barc_loss_weights.json', type=str, metavar='PATH',
                        help='name of json file which contains the loss weights')
    parser.add_argument('-lwr', '--loss-weight-ref-path', default='refinement_loss_weights.json', type=str, metavar='PATH',
                        help='name of json file which contains the loss weights for the refinement network')
    parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 4)')
    parser.add_argument('--snapshot', default=0, type=int,
                        help='save models for every #snapshot epochs (default: 0)')

    # argument that decides if we continue a training run (loading full network) or start from scratch (using only pretrained parts)
    subparsers = parser.add_subparsers(dest="command")   # parser.add_subparsers(help="subparsers")
    parser_start = subparsers.add_parser('start')      # start training
    parser_continue = subparsers.add_parser('continue')   # continue training

    # arguments that we only have if we start a new training run 
    #   remark: some parts can / need to be pretrained (stacked hourglass, 3d network)
    parser_start.add_argument('--model-file-hg', default='', type=str, metavar='PATH',
                        help='path to saved model weights (stacked hour glass)')
    parser_start.add_argument('--model-file-3d', default='', type=str, metavar='PATH',
                        help='path to saved model weights (2d-to-3d model)')
    parser_start.add_argument('--model-file-shape', default='', type=str, metavar='PATH',
                        help='path to saved model weights (resnet, shape branch)')

    # arguments that we only have if we continue training the full network 
    parser_continue.add_argument('--model-file-complete', default='', type=str, metavar='PATH',
                        help='path to saved model weights (full model)')
    parser_continue.add_argument('--new-optimizer', default=0, type=int,
                        help='should we restart the optimizer? 0:no, 1: yes (default: 0)')
    main(parser.parse_args())


