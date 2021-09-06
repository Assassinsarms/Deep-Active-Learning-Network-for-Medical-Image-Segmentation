from glob import glob
import os
import errno
import sys
import time
from collections import OrderedDict
import imageio

import numpy as np
from skimage.io import imread, imsave 
from hausdorff import hausdorff_distance
from sklearn.model_selection import train_test_split

import torch
import torchvision
import torch.nn as nn
import torch.optim as optim
import torchvision.transforms.functional as tf
import torch.utils.data as data
from torch.cuda import amp
import torch.backends.cudnn as cudnn

from tqdm import tqdm
import pickle
import pandas as pd

from train_val import train, validate
from loss import BCEDiceLoss
from metrics import *
from utils import *
from model import *
from dataset import *

def enable_dropout(model):
    """
    enable the dropout layers during test-time 
    """
    for m in model.modules():
        if m.__class__.__name__.startswith('Dropout'):
            m.train()

def mc_samples(data_loader, forward_passes, model, n_classes, n_samples, device, height, width):
    """
    a pool of unlabelled images is fed into the trained U-Net and a measure of uncertainty is computed for each unlabelled sample. 
    entropy is used as a measure of uncertainty and is taken across different predictions.  
    :param data_loader:
    :param forward_passes:
        number of monte-carlo samples/forward passes
    :param model:
        unet
    :param n_classes:
        number of classes in the dataset
    :param n_samples:
        number of samples in the test set
    :param device:
    """
    dropout_predictions = np.empty((0, n_samples, n_classes, height, width)) 
    for i in tqdm(range(forward_passes)):                         # perform x forward passes to obtain x monte-carlo predictions
        predictions_forward_pass = np.empty((0, n_classes, height, width))
        model.eval()
        enable_dropout(model)   
        with torch.no_grad():            
            for batch_idx, (data, labels) in enumerate(data_loader):
                data = data.to(device) 
                preds = model(data)
                preds = torch.sigmoid(preds).data.cpu().numpy()
                predictions_forward_pass = np.vstack((predictions_forward_pass, preds))   
        dropout_predictions = np.vstack((dropout_predictions, predictions_forward_pass[np.newaxis, :, :, :, :])) 
    mean = np.mean(dropout_predictions, axis=0) 
    epsilon = sys.float_info.min
    uncertainty_maps = -np.sum(mean*np.log(mean + epsilon), axis=1)
    avg_pred = np.mean(dropout_predictions, axis=0)
    return avg_pred, dropout_predictions, uncertainty_maps

def uncertainty_scoring(uncertainty_maps):
    """
    uncertainty maps are given an uncertainty score 
    :param uncertainty_maps: trained Unet model
    :return: numpy array with an avg uncertainty score for each image in the unlabelled set
    """
    uncertainty_scores = np.empty((uncertainty_maps.shape[0]))
    for i in range(uncertainty_maps.shape[0]):
        uncertainty_scores[i] = np.mean(uncertainty_maps[i])    
    overall_uncertainty_score = np.sum(uncertainty_maps)
    return uncertainty_scores, overall_uncertainty_score
    
def add_annotated_sample(to_be_annotated_index, labelled_img_paths, labelled_mask_paths, unlabelled_img_paths, unlabelled_mask_paths):
    """
    the labelled dataset is amended to include the samples to be annotated and the unlabelled dataset is amended to remove the samples to be annotated.
    :param labelled_dataset:
    :param to_be_annotated_index:
    :param labelled_img_paths:
    :param labelled_mask_paths:
    :param unlabelled_img_paths:
    :param unlabelled_mask_paths:
    :param batch_size:
    """
    samples_to_be_annotated = [unlabelled_img_paths[i] for i in to_be_annotated_index]
    annotation = [unlabelled_mask_paths[i] for i in to_be_annotated_index]
    new_labelled_img_paths = labelled_img_paths + samples_to_be_annotated
    new_labelled_mask_paths = labelled_mask_paths + annotation 
    new_unlabelled_img_paths = list(np.delete(unlabelled_img_paths, to_be_annotated_index))
    new_unlabelled_mask_paths = list(np.delete(unlabelled_mask_paths, to_be_annotated_index))
    return new_labelled_img_paths, new_labelled_mask_paths, new_unlabelled_img_paths, new_unlabelled_mask_paths

def main():
    # split data into train (labelled, 30%), unlabelled (active learning simulation, 50%), test (20%) - using 5000 slices
    random_bool = False                     # controls whether the labelled set is entered randomly - normal training or actively - active learning
    height, width = 160, 160
    n_classes = 3                           # edema, non-enhancing tumour, enhancing tumour
    nb_experiments = 2
    nb_active_learning_iter = 3            # number of active learning iterations
    nb_active_learning_iter_size = 5        # number of samples to be added to the training set after each active learning iteration
    FORWARD_PASSES = 3
    EPOCHS = 1000
    LEARNING_RATE = 1e-2
    EARLY_STOP = 15
    DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
    BATCH_SIZE = 32
    NUM_WORKERS = 0
    #SCALER = amp.GradScaler()
    TRAIN_IMG_DIR = glob(r'D:\\AI MSc Large Modules\\Masters_Project\\CODE\\Deep-Active-Learning-Network-for-Medical-Image-Segmentation\\data\\train_val\\img\\*')
    TRAIN_LABEL_DIR = glob(r'D:\\AI MSc Large Modules\\Masters_Project\\CODE\\Deep-Active-Learning-Network-for-Medical-Image-Segmentation\\data\\train_val\\label\\*')
    UNLABELLED_IMG_DIR = glob(r'D:\\AI MSc Large Modules\\Masters_Project\\CODE\\Deep-Active-Learning-Network-for-Medical-Image-Segmentation\\data\\unlabelled\\img\\*')
    UNLABELLED_LABEL_DIR = glob(r'D:\\AI MSc Large Modules\\Masters_Project\\CODE\\Deep-Active-Learning-Network-for-Medical-Image-Segmentation\\data\\unlabelled\\label\\*')
    TEST_IMG_DIR = glob(r'D:\\AI MSc Large Modules\\Masters_Project\\CODE\\Deep-Active-Learning-Network-for-Medical-Image-Segmentation\\data\\test\\img\\*')
    TEST_LABEL_DIR = glob(r'D:\\AI MSc Large Modules\\Masters_Project\\CODE\\Deep-Active-Learning-Network-for-Medical-Image-Segmentation\\data\\test\\label\\*')

    # rename directories
    labelled_img_paths, labelled_mask_paths = TRAIN_IMG_DIR, TRAIN_LABEL_DIR
    unlabelled_img_paths, unlabelled_mask_paths = UNLABELLED_IMG_DIR, UNLABELLED_LABEL_DIR
    test_img_paths, test_mask_paths = TEST_IMG_DIR, TEST_LABEL_DIR

    # model, optimiser
    model = UNet2D(in_channels=4, out_channels=3).to(DEVICE) 
    print("=> Creating 2D UNET Model")
    loss_fn = BCEDiceLoss()
    optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=LEARNING_RATE)

    # load weights from base training set
    model.load_state_dict(torch.load('models/base_trained/2DUNET.pth'))
    
    n_samples = len(unlabelled_img_paths)
    
    overall_results = []
    overall_start = time.time()
    for r in range(nb_experiments):        
        print("\n\n*****************EXPERIMENT " + str(r+1) + " IS STARTING********************")
        
        # initialise paths with original unlabelled and labelled datasets 
        labelled_img_paths, labelled_mask_paths = TRAIN_IMG_DIR, TRAIN_LABEL_DIR
        unlabelled_img_paths, unlabelled_mask_paths = UNLABELLED_IMG_DIR, UNLABELLED_LABEL_DIR
        test_img_paths, test_mask_paths = TEST_IMG_DIR, TEST_LABEL_DIR

        # initialise results tracker for each experiment
        experiment_iteration_results = []
        for i in range(nb_active_learning_iter):                    # how many times the active learning loops
            if random_bool == True:
                model_path = 'models/randomly_trained/2DUNET_iter' + str(i) + '.pth'
                results_path = 'random_learning_results'
                data_type = 'random'
            else:
                model_path = 'models/active_trained/2DUNET_iter' + str(i) + '.pth'
                results_path = 'active_learning_results'
                data_type = 'uncertain'

            print("\n---------STARTING AL ITERATION NUMBER " + str(i) + "----------")

            # load unlabelled dataset
            unlabelled_dataset = Dataset(unlabelled_img_paths, unlabelled_mask_paths)
            unlabelled_loader = torch.utils.data.DataLoader(
                unlabelled_dataset,
                batch_size=BATCH_SIZE,
                shuffle=False,
                pin_memory=True,
                drop_last=False)

            # select samples to be annotated by an expert, either randomly or based on an uncertainty measure
            if random_bool == True:
                to_be_added_random = np.random.choice(n_samples, nb_active_learning_iter_size)
                
                labelled_img_paths, labelled_mask_paths, unlabelled_img_paths, \
                    unlabelled_mask_paths = add_annotated_sample(to_be_added_random, labelled_img_paths, 
                                                        labelled_mask_paths, unlabelled_img_paths, unlabelled_mask_paths)
            else:
                print("Computing Monte Carlo predictions for unlabelled data ...\n")
                pred, dropout_predictions, uncertainty_maps = mc_samples(unlabelled_loader, FORWARD_PASSES, 
                                    model, n_classes, n_samples, DEVICE, height, width)
                uncertainty_scores, overall_uncertainty_score = uncertainty_scoring(uncertainty_maps)
                to_be_annotated_uncertain = uncertainty_scores.argsort()[::-1][:nb_active_learning_iter_size]       # sorting the samples with the top 5 uncertainties and indexing them
                
                labelled_img_paths, labelled_mask_paths, unlabelled_img_paths, \
                    unlabelled_mask_paths = add_annotated_sample(to_be_annotated_uncertain, labelled_img_paths, 
                                                        labelled_mask_paths, unlabelled_img_paths, unlabelled_mask_paths)

            print("Overall uncertainty score for active learning iteration " + str(i) + " is", overall_uncertainty_score, "\n")
            
            print(str(nb_active_learning_iter_size) + " " + data_type + " samples have been added to the training set \n")

            print("------------TRAINING FOR AL ITERATION NUMBER " + str(i) + "-----------")
            
            train_img_paths, val_img_paths, train_mask_paths, val_mask_paths = train_test_split(labelled_img_paths, 
                                            labelled_mask_paths, test_size=0.2, random_state=41)

            train_dataset = Dataset(train_img_paths, train_mask_paths)
            val_dataset = Dataset(val_img_paths, val_mask_paths)

            train_loader = torch.utils.data.DataLoader(
                train_dataset,
                batch_size=BATCH_SIZE,
                shuffle=False,
                pin_memory=True,
                drop_last=False)
            val_loader = torch.utils.data.DataLoader(
                val_dataset,
                batch_size=BATCH_SIZE,
                shuffle=False,
                pin_memory=True,
                drop_last=False)

            log = pd.DataFrame(index=[], columns=[
            'epoch', 'lr', 'loss', 'iou', 'dice', 'val_loss', 'val_iou', 'val_dice'
            ])

            best_iou = 0
            trigger = 0
            start = time.time()
            for epoch in range(EPOCHS):
                print("'Epoch [%d/%d]" %(epoch, EPOCHS))

                # train
                train_log = train(train_loader, model, loss_fn, optimizer, DEVICE)

                # validate
                val_log = validate(val_loader, model, loss_fn, DEVICE)

                print('loss %.4f - iou %.4f - dice - %.4f - val_loss %.4f - val_iou %.4f - val_dice %.4f'
                    %(train_log['loss'], train_log['iou'], train_log['dice'], val_log['loss'], val_log['iou'], val_log['dice']))
                
                tmp = pd.Series([
                    epoch,
                    LEARNING_RATE,
                    train_log['loss'],
                    train_log['iou'],
                    train_log['dice'],
                    val_log['loss'],
                    val_log['iou'],
                    val_log['dice'],
                ], index=['epoch', 'lr', 'loss', 'iou', 'dice', 'val_loss', 'val_iou', 'val_dice'])

                log = log.append(tmp, ignore_index=True)

                path = results_path + "/experiment_" + str(r+1) + "/AL_iteration_" + str(i)
                try:
                    os.makedirs(path)
                except OSError as exc:
                    if exc.errno != errno.EEXIST:
                        raise
                    pass
                log.to_csv(path + "/log" + ".csv", index=False)

                trigger += 1

                if val_log['iou'] > best_iou:
                    torch.save(model.state_dict(), model_path)
                    best_iou = val_log['iou']
                    print("=> best model has been saved")
                    trigger = 0

                # early stopping
                if trigger >= EARLY_STOP:
                    print("=> early stopping")
                    break
                torch.cuda.empty_cache()
            end = time.time()
            print('Training and validation for active learning iteration ' + str(i) + ' has taken ', (end - start)/60, 'minutes to complete')
    
            
            print("------------TESTING FOR ACTIVE LEARNING ITERATION " + str(i) + " -----------")

            test_dataset = Dataset(test_img_paths, test_mask_paths)
            test_loader = torch.utils.data.DataLoader(
                test_dataset,
                batch_size=BATCH_SIZE,
                shuffle=False,
                pin_memory=True,
                drop_last=False)

            model.eval()
            """
            obtain and save the label map generated by the model for each active learning iteration
            """
            with torch.no_grad():
                for batch_idx, (data, labels) in tqdm(enumerate(test_loader), total=len(test_loader)):
                    data = data.to(DEVICE) 
                    
                    preds = model(data)
                    preds = torch.sigmoid(preds).data.cpu().numpy()
                    
                    img_paths = test_img_paths[BATCH_SIZE*batch_idx:BATCH_SIZE*(batch_idx+1)]

                    for i in range(preds.shape[0]):
                        npName = os.path.basename(img_paths[i])
                        overNum = npName.find(".npy")
                        rgbName = npName[0:overNum]
                        rgbName = rgbName  + ".png"
                        rgbPic = np.zeros([160, 160, 3], dtype=np.uint8)
                        for idx in range(preds.shape[2]):
                            for idy in range(preds.shape[3]):
                                #(ED, peritumoral edema) (label 2) green
                                if preds[i,0,idx,idy] > 0.5: 
                                    rgbPic[idx, idy, 0] = 0
                                    rgbPic[idx, idy, 1] = 128
                                    rgbPic[idx, idy, 2] = 0
                                #(NET, non-enhancing tumor)(label 1) red
                                if preds[i,1,idx,idy] > 0.5:
                                    rgbPic[idx, idy, 0] = 255
                                    rgbPic[idx, idy, 1] = 0
                                    rgbPic[idx, idy, 2] = 0
                                #(ET, enhancing tumor)(label 4) yellow
                                if preds[i,2,idx,idy] > 0.5:
                                    rgbPic[idx, idy, 0] = 255
                                    rgbPic[idx, idy, 1] = 255
                                    rgbPic[idx, idy, 2] = 0

                        path = results_path + "/experiment_" + str(r+1) + "/AL_iteration_" + str(i) + "/output/"
                        try:
                            os.makedirs(path)
                        except OSError as exc:
                            if exc.errno != errno.EEXIST:
                                raise
                            pass
                        imsave(results_path + "/experiment_" + str(r+1) + "/AL_iteration_" + str(i) + "/output/" + rgbName, rgbPic, check_contrast=False)

            """
            calculate metrics: Dice, Sensitivity, PPV, Hausdorff for each AL iteration
            """
            wt_dices = []
            tc_dices = []
            et_dices = []
            wt_sensitivities = []
            tc_sensitivities = []
            et_sensitivities = []
            wt_ppvs = []
            tc_ppvs = []
            et_ppvs = []
            wt_Hausdorff = []
            tc_Hausdorff = []
            et_Hausdorff = []

            maskPath = glob("base_test_results/output/" + "GT\*.png")
            pbPath = glob(results_path + "/experiment_" + str(r+1) + "/AL_iteration_ "+ str(i) + "/output/" + "*.png")

            for myi in tqdm(range(len(maskPath))):
                mask = imread(maskPath[myi])
                pb = imread(pbPath[myi])

                wtmaskregion = np.zeros([mask.shape[0], mask.shape[1]], dtype=np.float32)
                wtpbregion = np.zeros([mask.shape[0], mask.shape[1]], dtype=np.float32)

                tcmaskregion = np.zeros([mask.shape[0], mask.shape[1]], dtype=np.float32)
                tcpbregion = np.zeros([mask.shape[0], mask.shape[1]], dtype=np.float32)

                etmaskregion = np.zeros([mask.shape[0], mask.shape[1]], dtype=np.float32)
                etpbregion = np.zeros([mask.shape[0], mask.shape[1]], dtype=np.float32)

                for idx in range(mask.shape[0]):
                    for idy in range(mask.shape[1]):
                        # As long as any channel of this pixel has a value, it means that this pixel does not belong to the foreground, that is, it belongs to the WT area 
                        if mask[idx, idy, :].any() != 0:
                            wtmaskregion[idx, idy] = 1
                        if pb[idx, idy, :].any() != 0:
                            wtpbregion[idx, idy] = 1
                        # As long as the first channel is 255, it can be judged to be the TC area, because the first channel of red and yellow are both 255, which is different from green 
                        if mask[idx, idy, 0] == 255:
                            tcmaskregion[idx, idy] = 1
                        if pb[idx, idy, 0] == 255:
                            tcpbregion[idx, idy] = 1
                        # As long as the second channel is 128, it can be judged to be the ET area 
                        if mask[idx, idy, 1] == 128:
                            etmaskregion[idx, idy] = 1
                        if pb[idx, idy, 1] == 128:
                            etpbregion[idx, idy] = 1

                #Start calculating WT - whole tumour
                dice = dice_coef(wtpbregion,wtmaskregion)
                wt_dices.append(dice)
                ppv_n = ppv(wtpbregion, wtmaskregion)
                wt_ppvs.append(ppv_n)
                Hausdorff = hausdorff_distance(wtmaskregion, wtpbregion)
                wt_Hausdorff.append(Hausdorff)
                sensitivity_n = sensitivity(wtpbregion, wtmaskregion)
                wt_sensitivities.append(sensitivity_n)

                # Start calculating TC - tumour core
                dice = dice_coef(tcpbregion, tcmaskregion)
                tc_dices.append(dice)
                ppv_n = ppv(tcpbregion, tcmaskregion)
                tc_ppvs.append(ppv_n)
                Hausdorff = hausdorff_distance(tcmaskregion, tcpbregion)
                tc_Hausdorff.append(Hausdorff)
                sensitivity_n = sensitivity(tcpbregion, tcmaskregion)
                tc_sensitivities.append(sensitivity_n)

                # Start calculating ET - enhancing tumour
                dice = dice_coef(etpbregion, etmaskregion)
                et_dices.append(dice)
                ppv_n = ppv(etpbregion, etmaskregion)
                et_ppvs.append(ppv_n)
                Hausdorff = hausdorff_distance(etmaskregion, etpbregion)
                et_Hausdorff.append(Hausdorff)
                sensitivity_n = sensitivity(etpbregion, etmaskregion)
                et_sensitivities.append(sensitivity_n)

            print("------------RESULTS FOR EXPERIMENT " + str(r+1) + " AND ACTIVE LEARNING ITERATION " + str(i) + " -----------")

            print('WT Dice: %.4f' % np.mean(wt_dices))
            print('TC Dice: %.4f' % np.mean(tc_dices))
            print('ET Dice: %.4f' % np.mean(et_dices))
            print("=============")
            print('WT PPV: %.4f' % np.mean(wt_ppvs))
            print('TC PPV: %.4f' % np.mean(tc_ppvs))
            print('ET PPV: %.4f' % np.mean(et_ppvs))
            print("=============")
            print('WT sensitivity: %.4f' % np.mean(wt_sensitivities))
            print('TC sensitivity: %.4f' % np.mean(tc_sensitivities))
            print('ET sensitivity: %.4f' % np.mean(et_sensitivities))
            print("=============")
            print('WT Hausdorff: %.4f' % np.mean(wt_Hausdorff))
            print('TC Hausdorff: %.4f' % np.mean(tc_Hausdorff))
            print('ET Hausdorff: %.4f' % np.mean(et_Hausdorff))
            print("=============")

            iteration_results = {'WT Dice': np.mean(wt_dices), 'TC Dice': np.mean(tc_dices), 'ET Dice': np.mean(et_dices), 
                                'WT PPV': np.mean(wt_ppvs), 'TC PPV': np.mean(tc_ppvs), 'ET PPV': np.mean(et_ppvs),
                                'WT sensitivity': np.mean(wt_sensitivities), 'TC sensitivity': np.mean(tc_sensitivities), 'ET sensitivity': np.mean(et_sensitivities),
                                'WT Hausdorff': np.mean(wt_Hausdorff), 'TC Hausdorff': np.mean(tc_Hausdorff), 'ET Hausdorff': np.mean(et_Hausdorff)}
            experiment_iteration_results.append(iteration_results)
        pickle.dump(experiment_iteration_results, open(results_path + '/experiment_' + str(r+1) + '/AL_iteration_' + str(i) + '/iteration.results', 'wb'), -1)     
        
        # calculate the mean metrics from every active learning iteration from one experiment
        avg_iteration_WT_Dice = [experiment_iteration_results[i]['WT Dice'] for i in range(len(experiment_iteration_results))]
        avg_iteration_TC_Dice = [experiment_iteration_results[i]['TC Dice'] for i in range(len(experiment_iteration_results))]
        avg_iteration_ET_Dice = [experiment_iteration_results[i]['ET Dice'] for i in range(len(experiment_iteration_results))]
        avg_iteration_WT_PPV = [experiment_iteration_results[i]['WT PPV'] for i in range(len(experiment_iteration_results))]
        avg_iteration_TC_PPV = [experiment_iteration_results[i]['TC PPV'] for i in range(len(experiment_iteration_results))]
        avg_iteration_ET_PPV = [experiment_iteration_results[i]['ET PPV'] for i in range(len(experiment_iteration_results))]
        avg_iteration_WT_sensitivities = [experiment_iteration_results[i]['WT sensitivities'] for i in range(len(experiment_iteration_results))]
        avg_iteration_TC_sensitivities = [experiment_iteration_results[i]['TC sensitivities'] for i in range(len(experiment_iteration_results))]
        avg_iteration_ET_sensitivities = [experiment_iteration_results[i]['ET sensitivities'] for i in range(len(experiment_iteration_results))]
        avg_iteration_WT_Hausdorff = [experiment_iteration_results[i]['WT Hausdorff'] for i in range(len(experiment_iteration_results))]
        avg_iteration_TC_Hausdorff = [experiment_iteration_results[i]['TC Hausdorff'] for i in range(len(experiment_iteration_results))]
        avg_iteration_ET_Hausdorff = [experiment_iteration_results[i]['ET Hausdorff'] for i in range(len(experiment_iteration_results))]
        
        # store the mean metrics from every active learning iteration from one experiment
        avg_experiment_results = {'WT Dice': avg_iteration_WT_Dice, 'TC Dice': avg_iteration_TC_Dice, 'ET Dice': avg_iteration_ET_Dice, 
                                'WT PPV': avg_iteration_WT_PPV, 'TC PPV': avg_iteration_TC_PPV, 'ET PPV': avg_iteration_ET_PPV,
                                'WT sensitivity': avg_iteration_WT_sensitivities, 'TC sensitivity': avg_iteration_TC_sensitivities, 'ET sensitivity': avg_iteration_ET_sensitivities,
                                'WT Hausdorff': avg_iteration_WT_Hausdorff, 'TC Hausdorff': avg_iteration_TC_Hausdorff, 'ET Hausdorff': avg_iteration_ET_Hausdorff}
        overall_results.append(avg_experiment_results) # add the results for each experiment
    
    pickle.dump(overall_results, open(results_path + 'overall'+'.results', 'wb'), -1)   
    overall_end = time.time()     
    print('Learning for ' + str(r+1) + 'experiments has taken ', (overall_end - overall_start)/60, 'minutes to complete')
if __name__ == '__main__':
    main()