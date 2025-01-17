from logging import log
import os
from random import sample
from sys import meta_path
import time
import random
import torch
import logging
import numpy as np
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter
from utils.average_meter_helper import AverageMeter

from PIL import Image
from io import BytesIO
from torchvision.transforms import ToTensor

# metric
from experiment.test_utils.metric import AP_N

from utils.loadConfig import load_cfg
from utils.log_helper import init_log, add_file_handler, print_speed

# get method
from experiment.triplet_utils.get_loss import get_loss
from experiment.triplet_utils.get_backbone import get_backbone
from experiment.triplet_utils.get_optimizer import get_optimizer
from experiment.triplet_utils.get_dataloader import get_train_dataloader

# load model (more eazy way to get model.)
from experiment.triplet_utils.load_model import load_model_test, load_model_test_yeild

# plt
import matplotlib.pyplot as plt

# init logger
logger = init_log("global")

"""
This file is implement for calculating the MAP@5/10/50/100 for the dataset from model.
"""

def test_model(model, test_dataloader, log_interval, device):
    """Test and Return the feature vector of all sample in dataset with its index.

    Args:
        cfg: (dict) 
            config file of the test precedure.
        model: (nn.module)
            loaded model
        test_dataloader: (torch.Dataloader)
            It should not be none! A non-triplet dataloader to validate data.
            It's sample protocal is:
                {
                    "img": target image,
                    "cls": target class, 
                    "other": other information,
                        {
                            "index" : index,
                        }
                }
        writer: (tensorboard writer)
        device: cuda or cpu

    Return:
        a list of dict:[
            {
                "cls": class label of the sample,
                "feature": feature vectuer of the result,
                "other": other information,
                {
                    "index": index of the sample in the dataset,
                }
            },
            ...,
            {
                "cls": class label of the sample,
                "feature": feature vectuer of the result,
                "other": other information,
                {
                    "index": index of the sample in the dataset,
                }
            }] 
    """
    logger.info("\n------------------------- Start Forwarding Dataset -------------------------\n")
    
    # epoch average meter
    avg_test = AverageMeter()

    # get test batch count
    current_test_batch = 0
    total_test_batch = len(test_dataloader)

    # to return list
    out_sample_list = []
    for batch_idx, batch_sample in enumerate(test_dataloader):
        # Skip last iteration to avoid the problem of having different number of tensors while calculating
        # averages (sizes of tensors must be the same for pairwise distance calculation)
        if batch_idx + 1 == len(test_dataloader):
            continue
        batch_size = test_dataloader.batch_size

        # switch to evaluation mode.
        for param in model.parameters():
            param.requires_grad = False
        model.eval()

        # start time counting
        batch_start_time_test = time.time()

        # Forward pass - compute embeddings
        imgs = batch_sample["img"]
        cls = batch_sample["cls"]
        indexs = batch_sample["other"]["index"]

        imgs = imgs.to(device)

        out_put = model(imgs)

        out_put.to("cpu")

        for i in range(batch_size):
            out_dict = {
                "cls": cls[i],
                "feature": out_put[i],
                "other": {
                    "index" : indexs[i]
                    },
            }
            out_sample_list.append(out_dict)
        
        # batch time & batch count
        current_test_batch += 1
        batch_time = time.time() - batch_start_time_test

        if current_test_batch % log_interval == 0:            
                print_speed(current_test_batch, batch_time, total_test_batch, "global")
    else:
        logger.info("\n------------------------- End Forwarding Dataset -------------------------\n")

    return out_sample_list

def evaluate_one(query_sample, all_sample_list, top_n=None):
    """Evaluate one sample to a sample list, return ranked result and truth label list.

    A sample is formed as a dict and following the protocol below:
        {
            "cls": class label of the sample,
            "feature": feature vectuer of the result,
            "other": other information,
                {
                    "index": index of the sample in the dataset,
                }
        }

    If the top_n is None, then reture all ranked result, if is not None, 
    return the top_n result.

    Args:
        query_sample: (dict) 
            A sample for search
        all_sample_list: (list of dict) 
            the database for search
        top_n: (int) 
            return the top_n ranked sample if specified.

    Return:
        ranked_result: (list of dict)
            The ranked result of the sample, followed protocal below:
            [{
                "cls": class label of the sample,
                "feature": feature vectuer of the result,
                "other": other information,
                    {
                        "index": index of the sample in the dataset,
                        "dist": distance from sample to query,
                        "label": 1 if correct retrive else 0,
                    }
            }, ... ,]
        label_list: (list)
            The performance label of the search algorithm.
                If the search result is correct, then the value is 1; 
                If the result is in correct then the value is 0.

    """
    query_cls = query_sample["cls"]
    query_idx = query_sample["other"]["index"]
    query_vec = query_sample["feature"]
        
    rank_result = []
    for i in range(len(all_sample_list)):
        # unpack & calculate dict & calculate truth label
        sample = all_sample_list[i]
        sample_cls = sample["cls"]
        sample_vec = sample["feature"]
        sample_idx = sample["other"]["index"]
        sample_distance = F.pairwise_distance(query_vec.unsqueeze(0), sample_vec.unsqueeze(0)).to("cpu")
        if int(sample_idx) == int(query_idx):
            continue
        sample_label = int(query_cls == sample_cls)

        # pack all
        sample_result = {
            "cls": sample_cls,
            "feature": sample_vec,
            "other":
                {
                    "index": sample_idx,
                    "dist": sample_distance,
                    "label": sample_label,
                }
        }

        rank_result.append(sample_result)
    
    # take second element for sort
    def take_dist(elem):
        return elem["other"]["dist"]

    rank_result.sort(key=take_dist)
    label_list = [i["other"]["label"] for i in rank_result]

    return label_list, rank_result

def evaluate_all_map(all_sample_list, sample_number=100, N=100, random_seed=1):
    """evaluate MAP@n value for all sample's database.

    random sample sample_number from all sample list, then calculate the mean AP@N result.

    Args:
        all_sample_list: (list of dict)
            The ranked result of the sample, followed protocal below:
            [{
                "cls": class label of the sample,
                "feature": feature vectuer of the result,
                "other": other information,
                {
                    "index": index of the sample in the dataset,
                }
            },
            ...,
            {
                "cls": class label of the sample,
                "feature": feature vectuer of the result,
                "other": other information,
                {
                    "index": index of the sample in the dataset,
                }
            },]
        sample_number: (int)
            Use how many sample to calculate MAP from the database.
        N: (int)
            Cal AP@N?
        random_seed: (int)
            use seed to sample
        
    Return:
        MAP_value: 
            The map performance for the retrive data.
    """
    # set seed
    random.seed(random_seed)

    # sample query
    query_samples = random.sample(all_sample_list, sample_number)

    # cal ap
    ap = []
    for query_sample in query_samples:
        label_list, _ = evaluate_one(query_sample, all_sample_list)
        ap_value = AP_N(label_list, N)
        ap.append(ap_value)
    
    # cal mAP
    MAP_value = sum(ap)/len(ap)

    return MAP_value

def visualization_one_retrieval(query_sample, all_sample_list, test_dataloader, top_n=10):
    """Visualize one retrieval from database.

    Visualization of one sample's top_n retieval result, return a plot instance.

    A sample is formed as a dict and following the protocol below:
        [{
            "cls": class label of the sample,
            "feature": feature vectuer of the result,
            "other": other information,
                {
                    "index": index of the sample in the dataset,
                }
        }]

    Args:
        query_sample: (dict) 
            A sample for search
        all_sample_list: (list of dict) 
            the database for search
        test_dataloader: (dataloader)
            A non-triplet dataloader to validate data.
            It's sample protocal is:
                {
                    "img": target image,
                    "cls": target class, 
                    "other": other information,
                        {
                            "index" : index,
                        }
                }
        top_n: (int) 
            return the top_n ranked sample if specified.

    Return:
        A plot instance.
    """
    label_list, rank_result = evaluate_one(query_sample, all_sample_list, top_n=top_n)
    """ Protocal of rank_result
    {
        "cls": class label of the sample,
        "feature": feature vectuer of the result,
        "other": other information,
            {
                "index": index of the sample in the dataset,
                "dist": distance from sample to query,
                "label": 1 if correct retrive else 0,
            }
    }
    """
    ap_value = AP_N(label_list, N=top_n)
    
    image_list = []
    title_list = []

    title_format = "number: {0} | cls: {3} \nis_true_result: {1} | dist:{2:.4f}"

    query_idx = query_sample["other"]["index"]
    query_img = test_dataloader.dataset.get_raw_image(query_idx)
    query_img_np = np.asarray(query_img)

    title_list.append("Query Image class: {0} \n AP: {1:.2f}".format(query_sample["cls"], ap_value))
    image_list.append(query_img_np)

    for i in range(len(rank_result[:top_n])):
        sample_cls = rank_result[i]["cls"]
        sample_dist = rank_result[i]["other"]["dist"]
        sample_index = rank_result[i]["other"]["index"]
        sample_label = rank_result[i]["other"]["label"]
        # PIL.Image
        sample_img = test_dataloader.dataset.get_raw_image(sample_index)
        sample_img_np = np.asarray(sample_img)

        title_list.append(title_format.format(i, sample_label, sample_dist.numpy()[0], sample_cls))
        image_list.append(sample_img_np)

    def grid_display(list_of_images, list_of_titles=[], no_of_columns=1, figsize=(1, 1)):
        fig = plt.figure(figsize=figsize)
        column = 0
        for i in range(len(list_of_images)):
            column += 1
            #  check for end of column and create a new figure
            if column == no_of_columns+1:
                fig = plt.figure(figsize=figsize)
                column = 1
            fig.add_subplot(1, no_of_columns, column)
            plt.imshow(list_of_images[i])
            plt.axis('off')
            if len(list_of_titles) >= len(list_of_images):
                plt.title(list_of_titles[i], fontsize='10', fontweight="semibold")

        # generate image
        buf = BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        
        # to tensor
        image = Image.open(buf)
        # for debug
        # image.save("./test.png")
        image = ToTensor()(image)
        return image

    return grid_display(image_list, title_list, top_n + 1, (35, 3))

def visualization_n_retrieval(all_sample_list, test_dataloader, visualize_num, top_n=10):
    """Visualize n sample's result for retrieval dataset

    Args:
        all_sample_list:(list of dict)
            the database for search
        test_dataloader:(dataloader)
            A non-triplet dataloader to validate data.
            It's sample protocal is:
                {
                    "img": target image,
                    "cls": target class, 
                    "other": other information,
                        {
                            "index" : index,
                        }
                }
        visualize_num: (int)
            The visulization sample number n.
        top_n:(int)
            return the top_n ranked sample if specified.

    Return:
        Return a list of figure image that can directly used for writer.add_image function.
    
    """
    query_image = random.sample(all_sample_list, visualize_num)
    figs = []
    for i in range(visualize_num):
        figs.append(visualization_one_retrieval(query_image[i], output_sample_list, test_dataloader, top_n=top_n))
    return figs

def generate_embedding(all_sample_list, test_dataloader, visualize_num=1000, random_seed=1):
    """Generate embedding projector data to summary. 

    Args:
        all_sample_list:(list of dict)
            the database for search
        test_dataloader:(dataloader)
            A non-triplet dataloader to validate data.
            It's sample protocal is:
                {
                    "cls": class label of the sample,
                    "feature": feature vectuer of the result,
                    "other": other information,
                    {
                        "index": index of the sample in the dataset,
                    }
                },
    Return:
        Generate embedding projector arguments.

        mat: (torch.Tensor or numpy.array) 
            A matrix which each row is the feature vector of the data point
        metadata: (list) 
            A list of labels, each element will be convert to string
        label_img: (torch.Tensor)
            Images correspond to each data point
    """
    # init all variable
    mat = []
    metadata = []
    label_img = []
    test_dataset = test_dataloader.dataset

    # set seed
    random.seed(random_seed)
    # random select datapoint
    part_sample_list = random.sample(all_sample_list, visualize_num)

    # extract target value
    for sample in part_sample_list:
        img_cls = int(sample["cls"])
        img_feature = sample["feature"]
        index = sample["other"]["index"]
        img_content = test_dataset[index]["img"]

        mat.append(img_feature)
        metadata.append(img_cls)
        label_img.append(img_content)

    # convert back to normal tensor
    mat_tensor = torch.stack(mat, dim=0)
    label_img_tensor = torch.stack(label_img, dim=0)

    return mat_tensor, metadata, label_img_tensor
            

if __name__ == "__main__":
    """
    单独Test模型使用
    """
    import argparse

    parser = argparse.ArgumentParser(description='Validate triplet network')

    # config file name
    parser.add_argument('--config_name', default='Arch_Dataset/Arch_Dataset_Resnet18_triplet_test.yml', type=str,
                        help='name of config file')

    args = parser.parse_args()

    # get config file name.
    config_name = args.config_name

    """
    Decide wether to use cuda, set random seed and init the logger & average meter,
    init experiment folder.
    """

    # get config folder
    curernt_file_path = os.path.dirname(os.path.abspath(__file__))
    experiment_config_folder = os.path.join(curernt_file_path, "config")

    # get cfg file
    cfg = load_cfg(experiment_config_folder, config_name)

    # 要从cfg里加载的东西
    # general的设置
    experiment_name = cfg["experiment_name"] 
    resume_name = cfg["resume_name"]
    experiment_seed = cfg["experiment_seed"]
    dont_use_cuda = cfg["dont_use_cuda"]
    # log的设置
    log_interval = cfg["log_interval"]

    # set cuda
    cuda = not dont_use_cuda and torch.cuda.is_available()
    device = torch.device("cuda" if cuda else "cpu")

    # set seed 
    torch.manual_seed(experiment_seed)
    if cuda:
        torch.cuda.manual_seed(experiment_seed)
    random.seed(experiment_seed)

    # Create experiment folder structure
    curernt_file_path = os.path.dirname(os.path.abspath(__file__))
    experiment_folder = os.path.join(curernt_file_path, "all_experiment", experiment_name)
    experiment_snap_folder = os.path.join(experiment_folder, "snap")
    experiment_board_folder = os.path.join(experiment_folder, "board_test")
    os.makedirs(experiment_folder, exist_ok=True)
    os.makedirs(experiment_snap_folder, exist_ok=True)
    os.makedirs(experiment_board_folder, exist_ok=True)


    # init board writer
    writer = SummaryWriter(experiment_board_folder)

    # get log
    add_file_handler("global", os.path.join(experiment_folder, 'test.log'), level=logging.INFO)


    # get dataset 
    dataset_pre_processing = []
    train_dataloader, test_dataloader = get_train_dataloader(cfg=cfg,
                                                            use_cuda=cuda, 
                                                            pre_process_transform=dataset_pre_processing)

    # load model
    # models, start_epochs = load_model_test(cfg, cuda)

    # result
    output_sample_lists = []
    mAP_500s = []
    mAP_100s = []
    mAP_10s = []


    # Test all models, and store the result in the lists
    for model, start_epoch in load_model_test_yeild(cfg, cuda):
        # start validte model
        output_sample_list = test_model(model, train_dataloader, log_interval, device)
        """
        {
            "cls": class label of the sample,
            "feature": feature vectuer of the result,
            "other": other information,
            {
                "index": index of the sample in the dataset,
            }
        },
        """
        # sample and calculate mAP@10
        logger.info("\n------------------------- Calculating mAP@10 -------------------------\n")
        mAP_10 = evaluate_all_map(output_sample_list, sample_number=50, N=10, random_seed=experiment_seed)
        logger.info("MAP@10: {}".format(mAP_10))
        writer.add_scalar("MAP@10", mAP_10, start_epoch)

        # sample and calculate mAP@100
        logger.info("\n------------------------- Calculating mAP@100 -------------------------\n")
        mAP_100 = evaluate_all_map(output_sample_list, sample_number=50, N=100, random_seed=experiment_seed)
        logger.info("MAP@100: {}".format(mAP_100))
        writer.add_scalar("MAP@100", mAP_100, start_epoch)

        # sample and calculate mAP@500
        logger.info("\n------------------------- Calculating mAP@500 -------------------------\n")
        mAP_500 = evaluate_all_map(output_sample_list, sample_number=50, N=500, random_seed=experiment_seed)
        logger.info("MAP@500: {}".format(mAP_500))
        writer.add_scalar("MAP@500", mAP_500, start_epoch)

        # add one epoch to all list
        output_sample_lists.append(output_sample_list)
        mAP_500s.append(mAP_500)
        mAP_100s.append(mAP_100)
        mAP_10s.append(mAP_10)

    # get best result
    best_epoch_idx = mAP_500s.index(max(mAP_500s))
    best_epoch_mAp_10 = mAP_10s[best_epoch_idx]
    best_epoch_mAp_100 = mAP_100s[best_epoch_idx]
    best_epoch_mAp_500 = mAP_500s[best_epoch_idx]
    best_output_sample_list = output_sample_lists[best_epoch_idx]

    # TODO: 是否要加入到cfg里面?
    # generate enbedding projection
    logger.info("\n------------------------- Calculating projection -------------------------\n")
    mat_tensor, metadata, label_img_tensor = generate_embedding(best_output_sample_list, train_dataloader, 500, random_seed=experiment_seed)
    writer.add_embedding(mat_tensor, metadata, label_img_tensor)

    # plot a random sample on board
    logger.info("\n------------------------- Visualizing n retrieval -------------------------\n")
    figs = visualization_n_retrieval(best_output_sample_list, train_dataloader, visualize_num=100, top_n=10)
    for i in range(len(figs)):
        writer.add_image("Test/random_retrieval_best_epoch_{}".format(best_epoch_idx), figs[i], i)


    # log best result and epoch
    logger.info("\n------------------------- Visualizing n retrieval -------------------------\n")
    logger.info("best epoch: {}".format(best_epoch_idx))
    logger.info("best epoch mAP@10: {}".format(best_epoch_mAp_10))
    logger.info("best epoch mAP@100: {}".format(best_epoch_mAp_100))
    logger.info("best epoch mAP@500: {}".format(best_epoch_mAp_500))

