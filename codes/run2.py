#!/usr/bin/env python3

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import json
import logging
import os
import zlib
import pickle
import re
from bidict import bidict

import torch

from torch.utils.data import DataLoader

from model import KGEModel

from dataloader import TrainDataset
from dataloader import BidirectionalOneShotIterator

def zdump(value,filename):
    with open(filename,"wb",-1) as fpz:
        fpz.write(zlib.compress(pickle.dumps(value,-1),9))

def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description='Training Knowledge Graph Embedding Models',
        usage='train.py [<args>] [-h | --help]'
    )

    parser.add_argument('--cuda', action='store_true', help='use GPU')
    
    parser.add_argument('--evaluate_train', action='store_true', help='Evaluate on training data')
    
    parser.add_argument('--countries', action='store_true', help='Use Countries S1/S2/S3 datasets')
    
    parser.add_argument('--data_path', type=str, default=None)
    parser.add_argument('--model', default='TransE', type=str)
    parser.add_argument('-de', '--double_entity_embedding', action='store_true')
    parser.add_argument('-dr', '--double_relation_embedding', action='store_true')
    
    parser.add_argument('-n', '--negative_sample_size', default=128, type=int)
    parser.add_argument('-d', '--hidden_dim', default=500, type=int)
    parser.add_argument('-g', '--gamma', default=12.0, type=float)
    parser.add_argument('-adv', '--negative_adversarial_sampling', action='store_true')
    parser.add_argument('-a', '--adversarial_temperature', default=1.0, type=float)
    parser.add_argument('-b', '--batch_size', default=1024, type=int)
    parser.add_argument('-r', '--regularization', default=0.0, type=float)
    parser.add_argument('--uni_weight', action='store_true', 
                        help='Otherwise use subsampling weighting like in word2vec')
    
    parser.add_argument('-lr', '--learning_rate', default=0.0001, type=float)
    parser.add_argument('-cpu', '--cpu_num', default=10, type=int)
    parser.add_argument('-init', '--init_checkpoint', default=None, type=str)
    parser.add_argument('-save', '--save_path', default=None, type=str)
    parser.add_argument('--max_steps', default=100000, type=int)
    parser.add_argument('--warm_up_steps', default=None, type=int)
    
    parser.add_argument('--save_checkpoint_steps', default=10000, type=int)
    parser.add_argument('--log_steps', default=100, type=int, help='train log every xx steps')
    
    parser.add_argument('--nentity', type=int, default=0, help='DO NOT MANUALLY SET')
    parser.add_argument('--nrelation', type=int, default=0, help='DO NOT MANUALLY SET')
    
    return parser.parse_args(args)

def override_config(args):
    '''
    Override model and data configuration
    '''
    
    with open(os.path.join(args.init_checkpoint, 'config.json'), 'r') as fjson:
        argparse_dict = json.load(fjson)
    
    args.countries = argparse_dict['countries']
    if args.data_path is None:
        args.data_path = argparse_dict['data_path']
    args.model = argparse_dict['model']
    args.double_entity_embedding = argparse_dict['double_entity_embedding']
    args.double_relation_embedding = argparse_dict['double_relation_embedding']
    args.hidden_dim = argparse_dict['hidden_dim']
    args.test_batch_size = argparse_dict['test_batch_size']
    
def save_model(model, optimizer, save_variable_list, args, entity2id, relation2id):
    '''
    Save the parameters of the model and the optimizer,
    as well as some other variables such as step and learning_rate
    '''
    
    argparse_dict = vars(args)
    print(argparse_dict)
    with open(os.path.join(args.save_path, 'config.json'), 'w') as fjson:
        json.dump(argparse_dict, fjson)

    torch.save({
        **save_variable_list,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict()},
        os.path.join(args.save_path, 'checkpoint')
    )
    
    entity_embedding = model.entity_embedding.detach().cpu().numpy()
    relation_embedding = model.relation_embedding.detach().cpu().numpy()
    zdump({"entity_embedding": entity_embedding,
           "relation_embedding": relation_embedding,
           "entity2id":entity2id,
           "relation2id":relation2id},
          os.path.join(args.save_path, "embedding.pkl"))

def set_logger(args):
    '''
    Write logs to checkpoint and console
    '''

    log_file = os.path.join(args.save_path or args.init_checkpoint, 'train.log')

    logging.basicConfig(
        format='%(asctime)s %(levelname)-8s %(message)s',
        level=logging.INFO,
        datefmt='%Y-%m-%d %H:%M:%S',
        filename=log_file,
        filemode='w'
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s')
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)

def log_metrics(mode, step, metrics):
    '''
    Print the evaluation logs
    '''
    for metric in metrics:
        logging.info('%s %s at step %d: %f' % (mode, metric, step, metrics[metric]))
        
        
def main(args):
    if args.init_checkpoint:
        override_config(args)
    elif args.data_path is None:
        raise ValueError('one of init_checkpoint/data_path must be choosed.')

    if args.save_path is None:
        raise ValueError('Where do you want to save your trained model?')
    
    if args.save_path and not os.path.exists(args.save_path):
        os.makedirs(args.save_path)
    
    # Write logs to checkpoint and console
    set_logger(args)

    with open(args.data_path) as fin:
        entity2id = bidict()
        relation2id = bidict()
        train_triples = []
        for line in fin:
            _tmp = [x.strip() for x in re.split("[,\t]", line) if x.strip()][:3]
            if len(_tmp) < 3:
                continue
            e1, relation, e2 = tuple(_tmp)
            if not e1 in entity2id:
                entity2id[e1] = len(entity2id)
            if not e2 in entity2id:
                entity2id[e2] = len(entity2id)
            if not relation in relation2id:
                relation2id[relation] = len(relation2id)
            train_triples.append((entity2id[e1], relation2id[relation], entity2id[e2]))

    nentity = len(entity2id)
    nrelation = len(relation2id)
    
    args.nentity = nentity
    args.nrelation = nrelation
    
    logging.info('Model: %s' % args.model)
    logging.info('Data Path: %s' % args.data_path)
    logging.info('#entity: %d' % nentity)
    logging.info('#relation: %d' % nrelation)
    
    logging.info('#train: %d' % len(train_triples))
    
    #All true triples
    all_true_triples = train_triples
    
    kge_model = KGEModel(
        model_name=args.model,
        nentity=nentity,
        nrelation=nrelation,
        hidden_dim=args.hidden_dim,
        gamma=args.gamma,
        double_entity_embedding=args.double_entity_embedding,
        double_relation_embedding=args.double_relation_embedding
    )
    
    logging.info('Model Parameter Configuration:')
    for name, param in kge_model.named_parameters():
        logging.info('Parameter %s: %s, require_grad = %s' % (name, str(param.size()), str(param.requires_grad)))

    if args.cuda:
        kge_model = kge_model.cuda()
    
    # Set training dataloader iterator
    train_dataloader_head = DataLoader(
        TrainDataset(train_triples, nentity, nrelation, args.negative_sample_size, 'head-batch'), 
        batch_size=args.batch_size,
        shuffle=True, 
        num_workers=max(1, args.cpu_num//2),
        collate_fn=TrainDataset.collate_fn
    )
    
    train_dataloader_tail = DataLoader(
        TrainDataset(train_triples, nentity, nrelation, args.negative_sample_size, 'tail-batch'), 
        batch_size=args.batch_size,
        shuffle=True, 
        num_workers=max(1, args.cpu_num//2),
        collate_fn=TrainDataset.collate_fn
    )
    
    train_iterator = BidirectionalOneShotIterator(train_dataloader_head, train_dataloader_tail)
    
    # Set training configuration
    current_learning_rate = args.learning_rate
    optimizer = torch.optim.Adam(
        filter(lambda p: p.requires_grad, kge_model.parameters()), 
        lr=current_learning_rate
    )
    if args.warm_up_steps:
        warm_up_steps = args.warm_up_steps
    else:
            warm_up_steps = args.max_steps // 2

    if args.init_checkpoint:
        # Restore model from checkpoint directory
        logging.info('Loading checkpoint %s...' % args.init_checkpoint)
        checkpoint = torch.load(os.path.join(args.init_checkpoint, 'checkpoint'))
        init_step = checkpoint['step']
        kge_model.load_state_dict(checkpoint['model_state_dict'])
        
        current_learning_rate = checkpoint['current_learning_rate']
        warm_up_steps = checkpoint['warm_up_steps']
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    else:
        logging.info('Ramdomly Initializing %s Model...' % args.model)
        init_step = 0
    
    step = init_step
    
    logging.info('Start Training...')
    logging.info('init_step = %d' % init_step)
    logging.info('learning_rate = %d' % current_learning_rate)
    logging.info('batch_size = %d' % args.batch_size)
    logging.info('negative_adversarial_sampling = %d' % args.negative_adversarial_sampling)
    logging.info('hidden_dim = %d' % args.hidden_dim)
    logging.info('gamma = %f' % args.gamma)
    logging.info('negative_adversarial_sampling = %s' % str(args.negative_adversarial_sampling))
    if args.negative_adversarial_sampling:
        logging.info('adversarial_temperature = %f' % args.adversarial_temperature)
    
    # Set valid dataloader as it would be evaluated during training
    
    training_logs = []
    
    #Training Loop
    for step in range(init_step, args.max_steps):
        
        log = kge_model.train_step(kge_model, optimizer, train_iterator, args)
        
        training_logs.append(log)
        
        if step >= warm_up_steps:
            current_learning_rate = current_learning_rate / 10
            logging.info('Change learning_rate to %f at step %d' % (current_learning_rate, step))
            optimizer = torch.optim.Adam(
                filter(lambda p: p.requires_grad, kge_model.parameters()), 
                lr=current_learning_rate
            )
            warm_up_steps = warm_up_steps * 3
        
        if step % args.save_checkpoint_steps == 0:
            save_variable_list = {
                'step': step, 
                'current_learning_rate': current_learning_rate,
                'warm_up_steps': warm_up_steps
            }
            save_model(kge_model, optimizer, save_variable_list, args, entity2id, relation2id)
            
        if step % args.log_steps == 0:
            metrics = {}
            for metric in training_logs[0].keys():
                metrics[metric] = sum([log[metric] for log in training_logs])/len(training_logs)
            log_metrics('Training average', step, metrics)
            training_logs = []
            
    save_variable_list = {
        'step': step, 
        'current_learning_rate': current_learning_rate,
        'warm_up_steps': warm_up_steps
    }
    save_model(kge_model, optimizer, save_variable_list, args, entity2id, relation2id)
        
    if args.evaluate_train:
        logging.info('Evaluating on Training Dataset...')
        metrics = kge_model.test_step(kge_model, train_triples, all_true_triples, args)
        log_metrics('Test', step, metrics)
        
if __name__ == '__main__':
    main(parse_args())
