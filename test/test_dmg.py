import matplotlib.pyplot as plt
import numpy as np
import glob
import pandas as pd
import torch
from torch import nn
from tqdm.auto import tqdm
from torchvision import transforms
from torchvision.utils import make_grid
from torch.utils.data import DataLoader
import torch.nn.functional as F
import numpy as np
import os
from mmcv import Config
from mmseg.apis import set_random_seed
from mmseg.datasets import build_dataset,build_dataloader
from mmseg.datasets.builder import DATASETS
from mmseg.datasets.custom import CustomDataset
from mmseg.models import build_segmentor
from mmseg.apis import train_segmentor
from mmseg.apis import inference_segmentor, init_segmentor, show_result_pyplot
from mmseg.core.evaluation import get_palette
from mmcv.runner import get_dist_info, init_dist, load_checkpoint
from mmcv.parallel import MMDataParallel, MMDistributedDataParallel
from mmseg.apis import multi_gpu_test, single_gpu_test
from PIL import Image
import torchvision
import mmcv
import mmseg
print(mmseg.__version__)
# Parse document
from os import path, rename, listdir
import glob
from scipy import stats
CUDA_LAUNCH_BLOCKING=1
torch.manual_seed(0)
import argparse

parser = argparse.ArgumentParser
parser.add_argument("--nw", type=str, default='pspnet',
					help="Network name.")
parser.add_argument("--nss", type=int, default=0,
					help="Cross validation subsets starting number.")
parser.add_argument("--nse", type=int, default=1,
					help="Cross validation subsets ending number.")
parser.add_argument("--task", type=str, default='single',
					help="Task name")
parser.add_argument("--cp", type=str, required=True, help="Config path.")
parser.add_argument("--dr", type=str, required=True, help="Data root.")
parser.add_argument("--split_csv", type=str, required=True, help="Split file.")
parser.add_argument("--save_path", type=str, required=True, help="Prediction save path.")
args = parser.parse_args()

def mask2rle(img):
	'''
	img: numpy array, 1 - mask, 0 - background
	Returns run length as string formated
	'''
	pixels = img.T.flatten()
	# pixels= img.flatten()
	pixels = np.concatenate([[0], pixels, [0]])
	runs = np.where(pixels[1:] != pixels[:-1])[0] + 1
	runs[1::2] -= runs[::2]
	return ' '.join(str(x) for x in runs)

def preprocess(img,labels):
	img = np.array(img)
	lab_1hot = np.zeros((img.shape[0],img.shape[1],len(labels)),dtype=np.bool)
	for i in range(len(labels)):
		lab_1hot[:,:,i] = img==labels[i]
	return lab_1hot

def pred2kaggle(pred_path,csv_read,csv_write,im_col,lab_col,labels,select_col=None):
	# read a csv file associated with the testing data and create Kaggle solution csv file
	# im_col: csv column idx that points to image files
	# lab_col: csv column idx that points to label files
	# select_col: csv column idx that points to selected files (default:None)
	df = pd.read_csv(csv_read,header=None)
	labFiles = list(df[lab_col])
	imFiles = list(df[im_col])
	if select_col is not None:
		imFiles = [imFiles[i] for i in range(len(imFiles)) if df[select_col][i]]
		labFiles = [labFiles[i] for i in range(len(labFiles)) if df[select_col][i]]
	
	data = pd.DataFrame()
	for i in range(len(imFiles)):
		file_name = labFiles[i][2:].split('\\')[-1]
		
		m = preprocess(Image.open(os.path.join(pred_path,file_name)),labels)
		for j in range(len(labels)):
			name = labFiles[i][:-4]+'_'+str(labels[j])
			temp = pd.DataFrame.from_records([
						{
							'ImageId': name,
							'EncodedPixels': mask2rle(m[:,:,j]),  
						}]
					)
			data = pd.concat([data, temp],ignore_index=True)
		print(i)
	if csv_write is not None:
		data.to_csv(csv_write, index=False)
	return data

if __name__ == '__main__':
	checkpoint_path = args.cp
	test_save_path = save_path
	network = args.nw
	task_name = args.task

	# Origanize dataset
	data_root = args.dr
	train_file_csv = path.join(data_root, "files_train.csv")
	test_file_csv = path.join(data_root, "files_test.csv")

	split_csv = args.split_csv
	test_images = []

	with open(split_csv, 'r') as f:
		lines = f.readlines()
		for line in lines:
			test_images.append(line.strip('\n'))
	test_images = list(test_images)
	print(test_images[0])
	print("Testing images for damage detection: ", len(test_images))

	if task_name == 'single':
		classes = ('Undefined','Undamaged', 'ConcreteDamage', 'ExposedRebar')
		palette = [[0,0,0], [128, 128, 128], [129, 127, 38], [120, 69, 125]]
		@DATASETS.register_module()
		class TokaidoDataset(CustomDataset):
			CLASSES = classes
			PALETTE = palette
			def __init__(self, split, **kwargs):
				super().__init__(img_suffix='_Scene.png', seg_map_suffix='.bmp', 
								 split=split, **kwargs)
				assert path.exists(self.img_dir) and self.split is not None
		for k in range(args.nss,args.nse+1):
			cfg_file = glob.glob(checkpoint_path+network+'/'+str(k)+'/*.py')[0]
			cfg = Config.fromfile(cfg_file)
			cfg.model.test_cfg.mode='slide'
			cfg.model.test_cfg.stride = (128,128)
			cfg.model.test_cfg.crop_size = (256,256)
			cfg.model.pretrained = None
			cfg.data.test.test_mode = True
			cfg.data.test.data_root = data_root
			cfg.data.test.img_dir = 'img_syn_raw/test'
			cfg.data.test.ann_dir='synthetic/labdmg'
			cfg.data.test.split = 'splits/test_dmg.txt'

			cfg.data.test.pipeline[1]=dict(
				type='MultiScaleFlipAug',
				img_scale=(1920, 1080),
				img_ratios = [0.5,1.0,1.5,2.0],
				flip=False,
				transforms=[
					dict(type='Resize', keep_ratio=True),
					dict(type='RandomFlip'),
					dict(
						type='Normalize',
						mean=[123.675, 116.28, 103.53],
						std=[58.395, 57.12, 57.375],
						to_rgb=True),
					dict(type='ImageToTensor', keys=['img']),
					dict(type='Collect', keys=['img'])
				])
			cfg.test_pipeline[1]=dict(
				type='MultiScaleFlipAug',
				img_scale=(1920, 1080),
				img_ratios = [0.5,1.0,1.5,2.0],
				flip=False,
				transforms=[
					dict(type='Resize', keep_ratio=True),
					dict(type='RandomFlip'),
					dict(
						type='Normalize',
						mean=[123.675, 116.28, 103.53],
						std=[58.395, 57.12, 57.375],
						to_rgb=True),
					dict(type='ImageToTensor', keys=['img']),
					dict(type='Collect', keys=['img'])
				])

			dataset = build_dataset(cfg.data.test)
			data_loader = build_dataloader(
				dataset,
				samples_per_gpu=1,
				workers_per_gpu=cfg.data.workers_per_gpu,
				dist=False,
				shuffle=False)

			# build the model and load checkpoint
			model = build_segmentor(cfg.model, train_cfg=None, test_cfg=cfg.get('test_cfg'))
			cp_file = glob.glob(checkpoint_path+network+'/'+str(k)+'/*.pth')[0]
			checkpoint = load_checkpoint(model, cp_file, map_location='cpu')
			model.CLASSES = classes
			model.PALETTE = palette
			model.cfg = cfg     
			model.to('cuda')
			model.eval()
			model = MMDataParallel(model, device_ids=[0])
			outputs = single_gpu_test(model, data_loader)    
			for j in range(len(outputs)):
				save_path = test_save_path+network+'/'+str(k)+'/'		
				if not os.path.exists(save_path):
					os.makedirs(save_path)
				save_file = save_path+test_images[j]+'.bmp'
				img = Image.fromarray(outputs[j].astype(np.uint8)).resize((640,360))
				img.save(save_file)

	elif task_name == 'mode':
		split_csv = '/home/groups/noh/icshm_data/data_proj1/Tokaido_dataset/splits/test_dmg.txt'

		# Ignore depth for the moment, we could preprocess depth in the future
		with open(split_csv, 'r') as f:
			lines = f.readlines()
			for line in lines:
				test_images.append(line.strip('\n'))
		test_images = list(test_images)
		print(test_images[0])
		print("Testing images for damage detection: ", len(test_images))
		networks = ['hrnet','ocrnet','pspnet','resnest','swin']
		# networks = ['hrnet','ocrnet','pspnet']
		num_subsets = args.nse - args.nss + 1
		for i in tqdm(range(len(test_images))):
			tmp_tensor = np.empty((len(networks)*num_subsets,360,640))
			idx = 0
			for j in range(len(networks)):
				for k in range(args.nss,args.nse+1):
					tmp_tensor[idx,:,:] = Image.open(test_save_path+networks[j]+'/'+str(k)+'/'+test_images[i]+'.bmp')
					idx+=1
			result = np.reshape(stats.mode(np.reshape(tmp_tensor[0:len(networks)*num_subsets,:,:],(len(networks)*num_subsets,360*640)),axis=0).mode,(360,640))
			result = Image.fromarray(result.astype(np.uint8))

			result.save(test_save_path+'ensemble/'+test_images[i]+'.bmp')

	elif task_name == 'label':
		pred_path =  test_save_path+'ensemble/' #path to the folder that contains predicted masks			
		if not os.path.exists(pred_path):
			os.makedirs(pred_path)
		csv_path = data_root #path to the Tokaido Dataset folder

		# component labels
		csv_read = os.path.join(csv_path,'files_test.csv')
		im_col = 0
		lab_col = 2
		select_col = 6
		labels = [2,3]
		csv_write = test_save_path+'damage_submission_sample.csv'
		pred2kaggle(pred_path,csv_read,csv_write,im_col,lab_col,labels,select_col)
