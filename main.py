import os
import shutil
import matplotlib.pyplot as plt
import matplotlib
import numpy as np
import torch
import torch.utils.data
# import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets

import argparse
import re
from dataHelper import DatasetFolder
from helpers import makedir
import model
import push
import prune
import train_and_test as tnt
import save
from log import create_logger
from preprocess import mean, std, preprocess_input_function
import random
from settings import class_specific

matplotlib.use("Agg")

parser = argparse.ArgumentParser()
parser.add_argument('-gpuid', nargs=1, type=str, default='0') # python3 main.py -gpuid=0,1,2,3
parser.add_argument('-experiment_run', nargs=1, type=str, default='0')
parser.add_argument("-latent", nargs=1, type=int, default=32)
parser.add_argument("-last_layer_weight", nargs=1, type=int, default=None)
parser.add_argument("-fa_coeff", nargs=1, type=float, default=None)
parser.add_argument("-model", type=str)
parser.add_argument("-base", type=str)
parser.add_argument("-train_dir", type=str)
parser.add_argument("-test_dir", type=str)
parser.add_argument("-push_dir", type=str)
parser.add_argument('-finer_dir', type=str)
parser.add_argument("-random_seed", nargs=1, type=int)
parser.add_argument("-topk_k", nargs=1, type=int)
args = parser.parse_args()
os.environ['CUDA_VISIBLE_DEVICES'] = args.gpuid[0]
latent_shape = args.latent[0]
experiment_run = args.experiment_run[0]
load_model_dir = args.model
base_architecture = args.base
last_layer_weight = args.last_layer_weight[0]
fa_coeff_manual = args.fa_coeff
topk_k = args.topk_k[0]

random_seed_number = args.random_seed[0]
torch.manual_seed(random_seed_number)
torch.cuda.manual_seed(random_seed_number)
np.random.seed(random_seed_number)
random.seed(random_seed_number)
torch.backends.cudnn.enabled=False
torch.backends.cudnn.deterministic=True

# bookkeeping namings and code
from settings import img_size, prototype_shape, num_classes, \
                     prototype_activation_function, add_on_layers_type, prototype_activation_function_in_numpy

if not base_architecture:
    from settings import base_architecture

base_architecture_type = re.match('^[a-z]*', base_architecture).group(0)

prototype_shape = (prototype_shape[0], latent_shape, prototype_shape[2], prototype_shape[3])
print("Protoype shape: ", prototype_shape)

model_dir = '/usr/xtmp/mammo/saved_models/' + base_architecture + '/' + experiment_run + '/'
print("saving models to: ", model_dir)
makedir(model_dir)
shutil.copy(src=os.path.join(os.getcwd(), __file__), dst=model_dir)
shutil.copy(src=os.path.join(os.getcwd(), 'settings.py'), dst=model_dir)
shutil.copy(src=os.path.join(os.getcwd(), base_architecture_type + '_features.py'), dst=model_dir)
shutil.copy(src=os.path.join(os.getcwd(), 'model.py'), dst=model_dir)
shutil.copy(src=os.path.join(os.getcwd(), 'train_and_test.py'), dst=model_dir)
log, logclose = create_logger(log_filename=os.path.join(model_dir, 'train.log'))
img_dir = os.path.join(model_dir, 'img')
makedir(img_dir)
weight_matrix_filename = 'outputL_weights'
prototype_img_filename_prefix = 'prototype-img'
prototype_self_act_filename_prefix = 'prototype-self-act'
proto_bound_boxes_filename_prefix = 'bb'

# load the data
from settings import train_dir, test_dir, train_push_dir, \
                     train_batch_size, test_batch_size, train_push_batch_size

normalize = transforms.Normalize(mean=mean,
                                 std=std)

if args.train_dir:
    print("inputting training dir")
    train_dir = args.train_dir
if args.test_dir:
    test_dir = args.test_dir
if args.push_dir:
    train_push_dir = args.push_dir
if args.finer_dir:
    finer_annotation_dir = args.finer_dir
    print("fine annotation set location: ", finer_annotation_dir)
else:
    finer_annotation_dir = None
    finer_train_loader = None

# all datasets
# train set
train_dataset = DatasetFolder(
    train_dir,
    augmentation=False,
    loader=np.load,
    extensions=("npy",),
    transform = transforms.Compose([
        torch.from_numpy,
    ]))
train_loader = torch.utils.data.DataLoader(
    train_dataset, batch_size=train_batch_size, shuffle=True,
    num_workers=4, pin_memory=False)

# finer train set
if finer_annotation_dir:
    finer_train_dataset = DatasetFolder(
        finer_annotation_dir,
        augmentation=False,
        loader=np.load,
        extensions=('npy',),
        transform = transforms.Compose([
            torch.from_numpy,
        ]))
    finer_train_loader = torch.utils.data.DataLoader(
        finer_train_dataset, batch_size=10, shuffle=True, num_workers=4, pin_memory=False)

# push set
train_push_dataset = DatasetFolder(
    root = train_push_dir,
    loader = np.load,
    extensions=("npy",),
    transform = transforms.Compose([
        torch.from_numpy,
    ]))
train_push_loader = torch.utils.data.DataLoader(
    train_push_dataset, batch_size=train_push_batch_size, shuffle=False,
    num_workers=4, pin_memory=False)

# test set
test_dataset =DatasetFolder(
    test_dir,
    loader=np.load,
    extensions=("npy",),
    transform = transforms.Compose([
        torch.from_numpy,
    ]))
test_loader = torch.utils.data.DataLoader(
    test_dataset, batch_size=test_batch_size, shuffle=False,
    num_workers=4, pin_memory=False)


# we should look into distributed sampler more carefully at torch.utils.data.distributed.DistributedSampler(train_dataset)
log('training set location: {0}'.format(train_dir))
log('training set size: {0}'.format(len(train_loader.dataset)))
log('push set location: {0}'.format(train_push_dir))
log('push set size: {0}'.format(len(train_push_loader.dataset)))
log('test set location: {0}'.format(test_dir))
log('test set size: {0}'.format(len(test_loader.dataset)))
log('batch size: {0}'.format(train_batch_size))
log("Using topk_k coeff from bash args: {0}, which is {1:.4}%".format(topk_k, float(topk_k)*100./(14*14))) # for prototype size 1x1 on 14x14 grid experminents


# construct the model
if load_model_dir:
    ppnet = torch.load(load_model_dir)
    log('starting from model: {0}'.format(load_model_dir))
else:
    ppnet = model.construct_PPNet(base_architecture=base_architecture,
                                  pretrained=True, img_size=img_size,
                                  prototype_shape=prototype_shape,
                                  topk_k=topk_k,
                                  num_classes=num_classes,
                                  prototype_activation_function=prototype_activation_function,
                                  add_on_layers_type=add_on_layers_type,
                                  last_layer_weight=last_layer_weight,
                                  class_specific=class_specific)

# if prototype_activation_function == 'linear':
#    ppnet.set_last_layer_incorrect_connection(incorrect_strength=0)
ppnet = ppnet.cuda()
ppnet_multi = torch.nn.DataParallel(ppnet)

# define optimizer
from settings import joint_optimizer_lrs, joint_lr_step_size
joint_optimizer_specs = \
[{'params': ppnet.features.parameters(), 'lr': joint_optimizer_lrs['features'], 'weight_decay': 1e-3}, # bias are now also being regularized
 {'params': ppnet.add_on_layers.parameters(), 'lr': joint_optimizer_lrs['add_on_layers'], 'weight_decay': 1e-3},
 {'params': ppnet.prototype_vectors, 'lr': joint_optimizer_lrs['prototype_vectors']},
]
joint_optimizer = torch.optim.Adam(joint_optimizer_specs)
joint_lr_scheduler = torch.optim.lr_scheduler.StepLR(joint_optimizer, step_size=joint_lr_step_size, gamma=0.1)

from settings import warm_optimizer_lrs
warm_optimizer_specs = \
[{'params': ppnet.add_on_layers.parameters(), 'lr': warm_optimizer_lrs['add_on_layers'], 'weight_decay': 1e-3},
 {'params': ppnet.prototype_vectors, 'lr': warm_optimizer_lrs['prototype_vectors']},
]
warm_optimizer = torch.optim.Adam(warm_optimizer_specs)

from settings import last_layer_optimizer_lr
last_layer_optimizer_specs = [{'params': ppnet.last_layer.parameters(), 'lr': last_layer_optimizer_lr}]
last_layer_optimizer = torch.optim.Adam(last_layer_optimizer_specs)

# weighting of different training losses
from settings import coefs

# for fa adjustment training only
if not (fa_coeff_manual is None):
    coefs['fine'] = fa_coeff_manual[0]
    print("Using fa coeff from bash args: {}".format(coefs['fine']))
else:
    print("Using fa coeff from settings: {}".format(coefs['fine']))

# number of training epochs, number of warm epochs, push start epoch, push epochs
from settings import num_train_epochs, num_warm_epochs, push_start, push_epochs

# train the model
log('start training')
import copy

train_auc = []
test_auc = []
currbest, best_epoch = 0, -1

for epoch in range(num_train_epochs):
    log('epoch: \t{0}'.format(epoch))

    if epoch < num_warm_epochs:
        tnt.warm_only(model=ppnet_multi, log=log)
        _ = tnt.train(model=ppnet_multi, dataloader=train_loader, optimizer=warm_optimizer,
                      class_specific=class_specific, coefs=coefs, log=log, finer_loader=finer_train_loader)
    else:
        tnt.joint(model=ppnet_multi, log=log)
        _ = tnt.train(model=ppnet_multi, dataloader=train_loader, optimizer=joint_optimizer,
                      class_specific=class_specific, coefs=coefs, log=log, finer_loader=finer_train_loader)
        joint_lr_scheduler.step()

    auc = tnt.test(model=ppnet_multi, dataloader=test_loader,
                    class_specific=class_specific, log=log)
    save.save_model_w_condition(model=ppnet, model_dir=model_dir, model_name=str(epoch) + 'nopush', accu=auc,
                                target_accu=0.00, log=log)

    train_auc.append(_)
    if currbest < auc:
        currbest = auc
        best_epoch = epoch
    log("\tcurrent best auc is: \t\t{} at epoch {}".format(currbest, best_epoch))
    test_auc.append(auc)
    plt.plot(train_auc, "b", label="train")
    plt.plot(test_auc, "r", label="test")
    plt.ylim(0.4, 1)
    plt.legend()
    plt.savefig(model_dir + 'train_test_auc.png')
    plt.close()


    if epoch >= push_start and epoch in push_epochs:
        push.push_prototypes(
            train_push_loader, # pytorch dataloader (must be unnormalized in [0,1])
            prototype_network_parallel=ppnet_multi, # pytorch network with prototype_vectors
            class_specific=class_specific,
            preprocess_input_function=preprocess_input_function, # normalize if needed
            prototype_layer_stride=1,
            root_dir_for_saving_prototypes=img_dir, # if not None, prototypes will be saved here
            epoch_number=epoch, # if not provided, prototypes saved previously will be overwritten
            prototype_img_filename_prefix=prototype_img_filename_prefix,
            prototype_self_act_filename_prefix=prototype_self_act_filename_prefix,
            proto_bound_boxes_filename_prefix=proto_bound_boxes_filename_prefix,
            save_prototype_class_identity=True,
            log=log,
            prototype_activation_function_in_numpy=prototype_activation_function_in_numpy)
        accu = tnt.test(model=ppnet_multi, dataloader=test_loader,
                        class_specific=class_specific, log=log)
        save.save_model_w_condition(model=ppnet, model_dir=model_dir, model_name=str(epoch) + 'push', accu=accu,
                                    target_accu=0.00, log=log)

        if prototype_activation_function != 'linear':
            tnt.last_only(model=ppnet_multi, log=log)
            for i in range(10):
                log('iteration: \t{0}'.format(i))
                _ = tnt.train(model=ppnet_multi, dataloader=train_loader, optimizer=last_layer_optimizer,
                              class_specific=class_specific, coefs=coefs, log=log, finer_loader=finer_train_loader)
                auc = tnt.test(model=ppnet_multi, dataloader=test_loader,
                                class_specific=class_specific, log=log)
                save.save_model_w_condition(model=ppnet, model_dir=model_dir, model_name=str(epoch) + '_' + str(i) + 'push', accu=auc,
                                            target_accu=0.00, log=log)
                train_auc.append(_)
                test_auc.append(auc)

                if currbest < auc:
                    currbest = auc
                    best_epoch = epoch

                plt.plot(train_auc, "b", label="train")
                plt.plot(test_auc, "r", label="test")
                plt.ylim(0.4, 1)
                plt.legend()
                plt.savefig(model_dir + 'train_test_auc' + ".png")
                plt.close()
   
logclose()

