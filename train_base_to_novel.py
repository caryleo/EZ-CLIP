import os
import torch.nn as nn
from datasets import Action_DATASETS
from torch.utils.data import DataLoader
from tqdm import tqdm
import argparse
import shutil
from pathlib import Path
import yaml
from dotmap import DotMap
import pprint
import time


import matplotlib.pyplot as plt
import numpy as np
import math
from utils.KLLoss import *
from test import validate
from utils.Augmentation import *
from utils.solver import _optimizer,  _lr_scheduler
from utils.tools import *
from utils.Text_Prompt import *
from utils.saving import *

class TextCLIP(nn.Module):
    def __init__(self, model) :
        super(TextCLIP, self).__init__()
        self.model = model

    def forward(self,text):
        return self.model.encode_text(text)

class ImageCLIP(nn.Module):
    def __init__(self, model) :
        super(ImageCLIP, self).__init__()
        self.model = model

    def forward(self,image):
        return self.model.encode_image(image)
    
def print_time(seconds):
    seconds = seconds % (24 * 3600)
    hour = seconds // 3600
    seconds %= 3600
    minutes = seconds // 60
    seconds %= 60
    return "%d:%02d:%02d" % (hour, minutes, seconds)


def generate_triplet_samples(image_embeddings, text_embeddings, list_ids):
    anchor_indices = []
    positive_indices = []
    negative_indices = []

    for idx, anchor_class in enumerate(list_ids):
        # Find positive and negative indices
        positive_indices_for_class = [i for i, id in enumerate(list_ids) if id == anchor_class]
        negative_indices_for_class = [i for i, id in enumerate(list_ids) if id != anchor_class]

        positive_idx = random.choice(positive_indices_for_class)
        negative_idx = random.choice(negative_indices_for_class)

        anchor_indices.append(idx)
        positive_indices.append(positive_idx)
        negative_indices.append(negative_idx)

    anchor_images = image_embeddings[anchor_indices]
    anchor_texts = text_embeddings[anchor_indices]
    positive_images = image_embeddings[positive_indices]
    positive_texts = text_embeddings[positive_indices]
    negative_images = image_embeddings[negative_indices]
    negative_texts = text_embeddings[negative_indices]

    return {
        'anchor_image': anchor_images,
        'anchor_text': anchor_texts,
        'positive_image': positive_images,
        'positive_text': positive_texts,
        'negative_image': negative_images,
        'negative_text': negative_texts
    }
    
    
def main():
    global args, best_prec1
    global global_step
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', '-cfg', default='')
    parser.add_argument('--traning_name', default='')
    args = parser.parse_args()
    with open(args.config, 'r') as f:
        config = yaml.load(f)
    args.traning_name = config['training_name']     
    working_dir = os.path.join(config['weight_save_dir'] , config['network']['type'], config['network']['arch'], config['data']['dataset'], args.traning_name)
    print('-' * 80)
    print(' ' * 20, "working dir: {}".format(working_dir))
    print('-' * 80)

    print('-' * 80)
    print(' ' * 30, "Config")
    pp = pprint.PrettyPrinter(indent=4)
    pp.pprint(config)
    print('-' * 80)

    config = DotMap(config)

    Path(working_dir).mkdir(parents=True, exist_ok=True)
    shutil.copy(args.config, working_dir)


    device = "cuda" if torch.cuda.is_available() else "cpu" # If using GPU then use mixed precision training.

    model, clip_state_dict = clip.load(config.network.arch, config, device=device,jit=False, tsm=config.network.tsm, T=config.data.num_segments,dropout=config.network.drop_out, emb_dropout=config.network.emb_dropout,pretrain=config.network.init, joint = config.network.joint) #Must set jit=False for training  ViT-B/32

    transform_train = get_augmentation(True,config)
    transform_val = get_augmentation(False,config)

    if config.data.randaug.N > 0:
        transform_train = randAugment(transform_train, config)


    print('train transforms: {}'.format(transform_train.transforms))
    print('val transforms: {}'.format(transform_val.transforms))
    model_text = TextCLIP(model)
    model_image = ImageCLIP(model)
    
    param_count_with_prompts = 0
    param_count_with_adapters = 0
    visual_param_count_with_adapters = 0
    visual_param_count_with_T_adapters = 0
    for name, param in model.named_parameters():
        if 'prompt' in name:
            param_count_with_prompts += param.numel()
        if 'Adapter' in name and not 'visual' in name:
            print('text---',name)
            param_count_with_adapters += param.numel()
        if 'Adapter' in name and 'visual' in name:
            print('visual--',name)
            visual_param_count_with_adapters += param.numel()
        if 'T_Adapter' in name and 'visual' in name:
            print('temporal visual--',name)
            visual_param_count_with_T_adapters += param.numel()
    param_count_with_prompts_in_million = param_count_with_prompts /1_000_000
    param_count_with_adapter_in_million = (param_count_with_adapters) /1_000_000
    T_visual_param_count_with_adapter_in_million = visual_param_count_with_T_adapters/1_000_000
    S_visual_param_count_with_adapter_in_million = (visual_param_count_with_adapters-visual_param_count_with_T_adapters)/1_000_000
    # Print the count
    print(f'Number of Trainable Parameters with "prompts" in their names: {param_count_with_prompts_in_million:.3f}')
    print(f'Number of Trainable Parameters with "text adapters" in their names: {param_count_with_adapter_in_million:.3f}')
    print(f'Number of Trainable Parameters with "S visual adapters" in their names: {S_visual_param_count_with_adapter_in_million:.3f}')
    print(f'Number of Trainable Parameters with "T visual adapters" in their names: {T_visual_param_count_with_adapter_in_million:.3f}')
    
            
    ###########################################
    for name, p in model.named_parameters():
        if 'prompt' not in name and 'Adapter' not in name:
            p.requires_grad = False

    ###########################################################
    parameters = filter(lambda p: p.requires_grad, model.parameters())
    parameters = sum([np.prod(p.size()) for p in parameters]) / 1_000_000
    print('CLIP_model Trainable Parameters: %.3fM' % parameters)
    
    ############################################################
    model_text = torch.nn.DataParallel(model_text).cuda()
    model_image = torch.nn.DataParallel(model_image).cuda()
    ############################## base dataset  loader ###################################
    train_data = Action_DATASETS(config.data.train_list,config.data.label_list,num_segments=config.data.num_segments,image_tmpl=config.data.image_tmpl,random_shift=config.data.random_shift,
                       transform=transform_train)
    train_loader = DataLoader(train_data,batch_size=config.data.batch_size,num_workers=config.data.workers,shuffle=True,pin_memory=False,drop_last=True)
    
    val_data = Action_DATASETS(config.data.val_list,config.data.label_list, random_shift=False,num_segments=config.data.num_segments,image_tmpl=config.data.image_tmpl,
                       transform=transform_val)
    val_loader = DataLoader(val_data,batch_size=config.data.batch_size,num_workers=config.data.workers,shuffle=False,pin_memory=False,drop_last=True)
    ###################################################################################################################################################
    
    ############################## novel loader ###################################
    novel_val_data = Action_DATASETS(config.data.novel_val_list,config.data.novel_label_list, random_shift=False,num_segments=config.data.num_segments,image_tmpl=config.data.image_tmpl,
                       transform=transform_val)
    novel_val_loader = DataLoader(novel_val_data,batch_size=config.data.batch_size,num_workers=config.data.workers,shuffle=False,pin_memory=False,drop_last=True)
    ###################################################################################################################################################
    
   
    

    if device == "cpu":
        model_text.float()
        model_image.float()
    else :
        clip.model.convert_weights(model_text) # Actually this line is unnecessary since clip by default already on float16
        clip.model.convert_weights(model_image)

    loss_img = KLLoss()
    loss_txt = KLLoss()
    loss_motion = Motion_loss()
    # loss_triplet = Triplet_loss()

    start_epoch = config.solver.start_epoch
    
    if config.pretrain:
        if os.path.isfile(config.pretrain):
            print(("=> loading checkpoint '{}'".format(config.pretrain)))
            checkpoint = torch.load(config.pretrain)
            model.load_state_dict(checkpoint['model_state_dict'],strict=False)
            del checkpoint
        else:
            print(("=> no checkpoint found at '{}'".format(config.resume)))
    
    if config.resume:
        if os.path.isfile(config.resume):
            print(("=> loading checkpoint '{}'".format(config.resume)))
            checkpoint = torch.load(config.resume)
            model.load_state_dict(checkpoint['model_state_dict'])
            start_epoch = checkpoint['epoch']
            print(("=> loaded checkpoint '{}' (epoch {})"
                   .format(config.evaluate, start_epoch)))
            del checkpoint
        else:
            print(("=> no checkpoint found at '{}'".format(config.pretrain)))

    classes, num_text_aug, text_dict = text_prompt(train_data,config.data.gpt_discription, config.data.use_llm)
    

    
    ##### Novel########
    novel_classes, novel_num_text_aug, novel_text_dict = text_prompt(novel_val_data,config.data.novel_gpt_discription, config.data.use_llm)    

    


    optimizer = _optimizer(config, model)
    lr_scheduler = _lr_scheduler(config, optimizer)

    loss=[]
    top_1_acc=[]
    novel_top_1_acc=[]    
    best_prec1 = 0.0
    novel_best_prec1 = 0.0
    if config.solver.evaluate:
        prec1 = validate(start_epoch, novel_val_loader , classes, device,model, config,num_text_aug,working_dir,'Base')
        return

    
    for k,v in model.named_parameters():
        if v.requires_grad:
            print('{}: {}'.format(k, v.requires_grad))

    for epoch in range(start_epoch, config.solver.epochs):
        print('------------------------------------------------------------------------')
        print('Epoch %d start ..'%epoch)
        
        model_image.train()
        model_text.train()
        tic = time.time()
        epoch_loss=[]
        
        for kkk,(prompt_images,list_id) in enumerate((train_loader)):
            if config.solver.type != 'monitor':
                if (kkk+1) == 1 or (kkk+1) % 10 == 0:
                    lr_scheduler.step(epoch + kkk / len(train_loader))
            optimizer.zero_grad()
            prompt_images = prompt_images.view((-1,config.data.num_segments,3)+prompt_images.size()[-2:])
            b,t,c,h,w = prompt_images.size()
            text_id = numpy.random.randint(num_text_aug,size=len(list_id))
            texts = torch.stack([text_dict[j][i,:] for i,j in zip(list_id,text_id)])

            prompt_images= prompt_images.to(device).view(-1,c,h,w ) # omit the Image.fromarray if the images already in PIL format, change this line to images=list_image if using preprocess inside the dataset class
            texts = texts.to(device)

            image_embedding = model_image(prompt_images) 
            image_embedding = image_embedding.view(b,t,-1)
            ################ calculate motion loss #########
            if config.use_motion_loss:
                loss_video_motion = loss_motion(image_embedding)
            ################################################
            image_embedding = image_embedding.mean(dim=1, keepdim=False)
            text_embedding = model_text(texts)

            if config.network.fix_text:
                text_embedding.detach_()

            logit_scale = model.logit_scale.exp()
            logits_per_image, logits_per_text = create_logits(image_embedding,text_embedding,logit_scale)

            ground_truth = torch.tensor(gen_label(list_id),dtype=image_embedding.dtype,device=device)
            loss_imgs = loss_img(logits_per_image,ground_truth)
            loss_texts = loss_txt(logits_per_text,ground_truth)
            # triplet_sample = generate_triplet_samples(image_embedding, text_embedding, list_id)
            # loss_triplets = loss_triplet(triplet_sample)
            
            if config.use_motion_loss:
                total_loss = (loss_imgs + loss_texts)/2 + loss_video_motion
            else:
                total_loss = (loss_imgs + loss_texts)/2  #+ loss_triplets
            
            
            epoch_loss.append(total_loss.item())
            total_loss.backward()

            if device == "cpu":
                optimizer.step()
            else:
                convert_models_to_fp32(model)
                optimizer.step()
                clip.model.convert_weights(model)
            
            if kkk%10==0:    
                if config.use_motion_loss:
                    print('Epoch:%d  iteration:%d/%d, total loss:%f, image loss:%f, text loss:%f, motion loss:%f, lr:%f '%(epoch,kkk,len(train_loader),total_loss.item(),loss_imgs.item(),loss_texts.item(), loss_video_motion.item(), optimizer.param_groups[0]['lr']))
                else:
                    print('Epoch:%d  iteration:%d/%d, total loss:%f, image loss:%f, text loss:%f, lr:%f '%(epoch,kkk,len(train_loader),total_loss.item(),loss_imgs.item(),loss_texts.item(), optimizer.param_groups[0]['lr']))

        if epoch % config.logging.eval_freq == 0:  # and epoch>0
            print('novel accuracy')
            novel_prec1 = validate(epoch,novel_val_loader, novel_classes, device, model, config,novel_num_text_aug,working_dir,'Novel')
            print('Base val accuracy')
            prec1 = validate(epoch,val_loader, classes, device, model, config,num_text_aug,working_dir,'Base')
        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)
        print('Base Testing: {}/{}'.format(prec1,best_prec1))
        
        novel_is_best = novel_prec1 > novel_best_prec1
        novel_best_prec1 = max(novel_prec1, novel_best_prec1)
        print('Novel Testing: {}/{}'.format(novel_prec1,novel_best_prec1))
        txt_path = '{}/log.txt'.format(working_dir)
        if os.path.exists(txt_path):
            with open(txt_path, 'a+') as f:
                f.write('\n')
                if config.use_motion_loss:
                    f.write('Epoch:%d  iteration:%d/%d, total loss:%f, image loss:%f, text loss:%f, motion loss:%f, lr:%f \n'%(epoch,kkk,len(train_loader),total_loss.item(),loss_imgs.item(),loss_texts.item(), loss_video_motion.item(), optimizer.param_groups[0]['lr']))
                else:
                    f.write('Epoch:%d  iteration:%d/%d, total loss:%f, image loss:%f, text loss:%f, lr:%f \n'%(epoch,kkk,len(train_loader),total_loss.item(),loss_imgs.item(),loss_texts.item(), optimizer.param_groups[0]['lr']))
                f.write('Base Testing: {}/{}\n'.format(prec1,best_prec1))
                f.write('Novel Testing: {}/{}\n'.format(novel_prec1,novel_best_prec1))
                f.close()
        else:
            with open(txt_path, mode="wt") as f:
                if config.use_motion_loss:
                    f.write('Epoch:%d  iteration:%d/%d, total loss:%f, image loss:%f, text loss:%f, motion loss:%f, lr:%f \n'%(epoch,kkk,len(train_loader),total_loss.item(),loss_imgs.item(),loss_texts.item(), loss_video_motion.item(), optimizer.param_groups[0]['lr']))
                else:
                    f.write('Epoch:%d  iteration:%d/%d, total loss:%f, image loss:%f, text loss:%f, lr:%f \n'%(epoch,kkk,len(train_loader),total_loss.item(),loss_imgs.item(),loss_texts.item(), optimizer.param_groups[0]['lr']))
                f.write('Base Testing: {}/{}\n'.format(prec1,best_prec1))
                f.write('Novel Testing: {}/{}\n'.format(novel_prec1,novel_best_prec1))
                f.close()
        
        print('Saving:')
        filename = "{}/last_model.pt".format(working_dir,epoch)
        top_1_acc.append(prec1/100)
        novel_top_1_acc.append(novel_prec1/100)
        loss.append(np.mean(epoch_loss))
        epoch_saving(epoch, model, optimizer, filename)
        if is_best:
            print('Saving best weight based on Base accuracy at epoch %d'%epoch)
            best_saving(working_dir, epoch, model, optimizer, 'base')
        if novel_is_best:
            print('Saving best weight based on Novel accuracy at epoch %d'%epoch)
            best_saving(working_dir, epoch, model, optimizer, 'novel')
        print('Epoch %d end ..'%epoch)
        ##############graph_plot################
        X = list(range(len(loss)))
        plt.plot(X, loss, color='r', label='Training loss')
        plt.plot(X, top_1_acc, color='g', label='base Accuracy')
        plt.plot(X, novel_top_1_acc, color='m', label='Novel Accuracy')
        plt.xlabel("Epoch")
        plt.ylabel("Training loss and Accuracy")
        plt.title("Traing graph")
        plt.legend()
        plt.savefig('{}/Graph_plot.png'.format(working_dir))
        plt.close()
        print('Time taken by epoch %d:'%epoch, print_time(time.time()-tic))
        print('------------------------------------------------------------------------')
if __name__ == '__main__':
    main()
