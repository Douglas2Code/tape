import typing
import sys
import os
import logging
from pathlib import Path
import json
import itertools
from tqdm import tqdm
import argparse
import warnings
import pickle as pkl
import inspect

import torch
import torch.nn as nn

import tape_pytorch.models as models

try:
    import apex  # noqa: F401
    APEX_FOUND = True
except ImportError:
    APEX_FOUND = False

from tape_pytorch.registry import registry
import tape_pytorch.training as training
import tape_pytorch.utils as utils

CallbackList = typing.Sequence[typing.Callable]
OutputDict = typing.Dict[str, typing.List[typing.Any]]


logger = logging.getLogger(__name__)
warnings.filterwarnings(  # Ignore pytorch warning about loss gathering
    'ignore', message='Was asked to gather along dimension 0', module='torch.nn.parallel')


def create_base_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Parent parser for tape functions',
                                     add_help=False)
    parser.add_argument('model_type', choices=list(models.KNOWN_MODELS),
                        help='Base model class to run')
    parser.add_argument('--model-config-file', default=None, type=utils.check_is_file,
                        help='Config file for model')
    parser.add_argument('--data-dir', default='./data', type=utils.check_is_dir,
                        help='Directory from which to load task data')
    parser.add_argument('--vocab-file', default='data/pfam.model', type=utils.check_is_file,
                        help='Pretrained tokenizer vocab file')
    parser.add_argument('--output-dir', default='./results', type=str)
    parser.add_argument('--no-cuda', action='store_true', help='CPU-only flag')
    parser.add_argument('--seed', default=42, type=int, help='Random seed to use')
    parser.add_argument('--local_rank', type=int, default=-1,
                        help='Local rank of process in distributed training. '
                             'Set by launch script.')
    parser.add_argument('--tokenizer', choices=['bpe', 'amino_acid'], default='amino_acid',
                        help='Tokenizes to use on the amino acid sequences')
    parser.add_argument('--num-workers', default=8, type=int,
                        help='Number of workers to use for multi-threaded data loading')
    parser.add_argument('--log-level', default=logging.INFO,
                        choices=['DEBUG', 'INFO', 'WARN', 'WARNING', 'ERROR',
                                 logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR],
                        help="log level for the experiment")
    parser.add_argument('--debug', action='store_true', help='Run in debug mode')

    return parser


def create_train_parser(base_parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run Training on the TAPE datasets',
                                     parents=[base_parser])
    parser.add_argument('task', choices=list(registry.dataset_name_mapping.keys()),
                        help='TAPE Task to train/eval on')
    parser.add_argument('--learning-rate', default=1e-4, type=float,
                        help='Learning rate')
    parser.add_argument('--batch-size', default=1024, type=int,
                        help='Batch size')
    parser.add_argument('--num-train-epochs', default=10, type=int,
                        help='Number of training epochs')
    parser.add_argument('--num-log-iter', default=20, type=int,
                        help='Number of training steps per log iteration')
    parser.add_argument('--fp16', action='store_true', help='Whether to use fp16 weights')
    parser.add_argument('--warmup-steps', default=10000, type=int,
                        help='Number of learning rate warmup steps')
    parser.add_argument('--gradient-accumulation-steps', default=1, type=int,
                        help='Number of forward passes to make for each backwards pass')
    parser.add_argument('--loss-scale', default=0, type=int,
                        help='Loss scaling. Only used during fp16 training.')
    parser.add_argument('--max-grad-norm', default=1.0, type=float,
                        help='Maximum gradient norm')
    parser.add_argument('--exp-name', default=None, type=str,
                        help='Name to give to this experiment')
    parser.add_argument('--from-pretrained', default=None, type=utils.check_is_dir,
                        help='Directory containing config and pretrained model weights')
    parser.add_argument('--log-dir', default='./logs', type=str)
    parser.add_argument('--no-eval', action='store_true',
                        help='Flag to not run eval pass. Useful for gridsearching.')
    parser.add_argument('--save-freq', default=1, type=utils.int_or_str,
                        help="How often to save the model during training. Either an integer "
                             "frequency or the string 'improvement'")
    parser.add_argument('--patience', default=-1, type=int,
                        help="How many epochs without improvement to wait before ending "
                             "training")
    parser.add_argument('--resume-from-checkpoint', action='store_true',
                        help="whether to resume training from the checkpoint")
    return parser


def create_eval_parser(base_parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run Eval on the TAPE Datasets',
                                     parents=[base_parser])
    parser.add_argument('task', choices=list(registry.dataset_name_mapping.keys()),
                        help='TAPE Task to train/eval on')
    parser.add_argument('from_pretrained', type=utils.check_is_dir,
                        help='Directory containing config and pretrained model weights')
    parser.add_argument('--batch-size', default=1024, type=int,
                        help='Batch size')
    parser.add_argument('--save-callback', default=['save_predictions'],
                        help=f'Callbacks to use when saving. '
                             f'Choices: {list(registry.callback_name_mapping.keys())}',
                        nargs='*')
    parser.add_argument('--metrics', default=[],
                        help=f'Metrics to run on the result. '
                             f'Choices: {list(registry.metric_name_mapping.keys())}',
                        nargs='*')
    parser.add_argument('--split', default='test', type=str,
                        help='Which split to run on')
    return parser


def create_embed_parser(base_parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Embed a set of proteins wiht a pretrained model',
        parents=[base_parser])
    parser.add_argument('datafile', type=str,
                        help='File containing set of proteins to embed')
    parser.add_argument('outfile', type=str,
                        help='Name of output file')
    parser.add_argument('from_pretrained', type=utils.check_is_dir,
                        help='Directory containing config and pretrained model weights')
    parser.add_argument('--batch-size', default=1024, type=int,
                        help='Batch size')
    parser.set_defaults(task='embed')
    return parser


def create_distributed_parser(base_parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, parents=[base_parser])
    # typing.Optional arguments for the launch helper
    parser.add_argument("--nnodes", type=int, default=1,
                        help="The number of nodes to use for distributed "
                             "training")
    parser.add_argument("--node_rank", type=int, default=0,
                        help="The rank of the node for multi-node distributed "
                             "training")
    parser.add_argument("--nproc_per_node", type=int, default=1,
                        help="The number of processes to launch on each node, "
                             "for GPU training, this is recommended to be set "
                             "to the number of GPUs in your system so that "
                             "each process can be bound to a single GPU.")
    parser.add_argument("--master_addr", default="127.0.0.1", type=str,
                        help="Master node (rank 0)'s address, should be either "
                             "the IP address or the hostname of node 0, for "
                             "single node multi-proc training, the "
                             "--master_addr can simply be 127.0.0.1")
    parser.add_argument("--master_port", default=29500, type=int,
                        help="Master node (rank 0)'s free port that needs to "
                             "be used for communciation during distributed "
                             "training")
    return parser


def run_train(args: typing.Optional[argparse.Namespace] = None, env=None) -> None:
    if env is not None:
        os.environ = env

    if args is None:
        base_parser = create_base_parser()
        train_parser = create_train_parser(base_parser)
        args = train_parser.parse_args()

    if args.gradient_accumulation_steps < 1:
        raise ValueError(
            f"Invalid gradient_accumulation_steps parameter: "
            f"{args.gradient_accumulation_steps}, should be >= 1")

    if (args.fp16 or args.local_rank != -1) and not APEX_FOUND:
        raise ImportError(
            "Please install apex from https://www.github.com/nvidia/apex "
            "to use distributed and fp16 training.")

    arg_dict = vars(args)
    arg_names = inspect.getfullargspec(training.run_train).args

    missing = set(arg_names) - set(arg_dict.keys())
    if missing:
        raise RuntimeError(f"Missing arguments: {missing}")
    train_args = {name: arg_dict[name] for name in arg_names}

    training.run_train(**train_args)


def run_eval(args: typing.Optional[argparse.Namespace] = None) -> typing.Dict[str, float]:
    if args is None:
        base_parser = create_base_parser()
        parser = create_eval_parser(base_parser)
        args = parser.parse_args()

    if args.from_pretrained is None:
        raise ValueError("Must specify pretrained model")
    if args.local_rank != -1:
        raise ValueError("TAPE does not support distributed validation pass")

    device, n_gpu, is_master = utils.setup_distributed(args.local_rank, args.no_cuda)

    utils.setup_logging(args.local_rank, save_path=None, log_level=args.log_level)
    utils.set_random_seeds(args.seed, n_gpu)

    pretrained_dir = Path(args.from_pretrained)

    logger.info(
        f"device: {device} "
        f"n_gpu: {n_gpu}")

    model = models.get(args.model_type, args.task, args.model_config_file, args.from_pretrained)

    if n_gpu > 1:
        model = nn.DataParallel(model)  # type: ignore

    runner = training.ForwardRunner(model, device, n_gpu)
    valid_dataset = utils.setup_dataset(args.task, args.data_dir, args.split, args.tokenizer)
    valid_loader = utils.setup_loader(
        args.task, valid_dataset, args.batch_size, args.local_rank, n_gpu,
        1, args.num_workers)

    save_callbacks = [registry.get_callback(name) for name in args.save_callback]

    if len(args.metrics) > 0 and 'save_predictions' not in args.save_callback:
        save_callbacks.append(registry.get_callback('save_predictions'))
    metric_functions = [registry.get_metric(name) for name in args.metrics]

    save_outputs = training.run_eval_epoch(valid_loader, runner, is_master, save_callbacks)

    target_key = getattr(model, 'module', model).target_key
    prediction_key = getattr(model, 'module', model).prediction_key
    metrics = {name: metric(save_outputs[target_key], save_outputs[prediction_key])
               for name, metric in zip(args.metrics, metric_functions)}
    save_outputs.update(metrics)
    logger.info(f'Evaluation Metrics: {metrics}')

    with (pretrained_dir / 'results.pkl').open('wb') as f:
        pkl.dump(save_outputs, f)

    return metrics


def run_embed(args: typing.Optional[argparse.Namespace] = None) -> None:
    if args is None:
        base_parser = create_base_parser()
        parser = create_embed_parser(base_parser)
        args = parser.parse_args()

    if args.from_pretrained is None:
        raise ValueError("Must specify pretrained model")
    if args.local_rank != -1:
        raise ValueError("TAPE does not support distributed embed pass")

    device, n_gpu, is_master = utils.setup_distributed(args.local_rank, args.no_cuda)

    utils.setup_logging(args.local_rank, save_path=None, log_level=args.log_level)
    utils.set_random_seeds(args.seed, n_gpu)

    logger.info(
        f"device: {device} "
        f"n_gpu: {n_gpu}")

    model = models.get(
        args.model_type, args.task, args.model_config_file, args.from_pretrained)

    if n_gpu > 1:
        model = nn.DataParallel(model)  # type: ignore

    dataset = utils.setup_dataset(args.task, args.data_dir, args.datafile, args.tokenizer)
    loader = utils.setup_loader(
        args.task, dataset, args.batch_size, args.local_rank, n_gpu, 1, args.num_workers)

    torch.set_grad_enabled(False)
    model.eval()

    save_outputs = []
    save_callback = registry.get_callback('save_embedding')

    for batch in tqdm(loader, desc='Embedding sequences', total=len(loader),
                      disable=not is_master):
        cuda_batch = {name: tensor.cuda(device=device, non_blocking=True)
                      for name, tensor in batch.items()}
        outputs = model(**cuda_batch)

        to_save = save_callback(model, batch, outputs)
        save_outputs.append(to_save)

    keys = save_outputs[0].keys()
    output_dict = {
        key: list(itertools.chain.from_iterable(output[key] for output in save_outputs))
        for key in keys}

    with (Path(args.outfile).with_suffix('.pkl')).open('wb') as f:
        pkl.dump(output_dict, f)


def run_train_distributed(args: typing.Optional[argparse.Namespace] = None) -> None:
    """Runs distributed training via multiprocessing.
    """
    if args is None:
        base_parser = create_base_parser()
        distributed_parser = create_distributed_parser(base_parser)
        distributed_train_parser = create_train_parser(distributed_parser)
        args = distributed_train_parser.parse_args()

    # Define the experiment name here, instead of dealing with barriers and communication
    # when getting the experiment name
    exp_name = utils.get_expname(args.exp_name, args.task, args.model_type)
    args.exp_name = exp_name
    utils.launch_process_group(
        run_train, args, args.nproc_per_node, args.nnodes,
        args.node_rank, args.master_addr, args.master_port)


def run_gridsearch(args: typing.Optional[argparse.Namespace] = None, env=None) -> None:
    import random
    from copy import copy

    if env is not None:
        os.environ = env

    if args is None:
        parser = argparse.ArgumentParser()
        parser.add_argument('config_file', type=argparse.FileType('r'))
        gridsearch_args = parser.parse_args()
        config = json.load(gridsearch_args.config_file)
        gridsearch_args.config_file.close()

        fixed_values = {}
        grid_values = {}

        for key, value in config.items():
            if isinstance(value, list) and key != 'metrics':
                grid_values[key] = value
            else:
                fixed_values[key] = value

        args = argparse.Namespace(**fixed_values)

    args.log_level = 'WARN'
    args.exp_name = 'gridsearch' + "_{:0>6d}".format(random.randint(0, int(1e6)))
    args.save_callback = []

    gridsearch_logger = logging.getLogger('gridsearch')
    gridsearch_logger.setLevel(logging.INFO)
    gridsearch_handler = logging.StreamHandler(sys.stdout)
    gridsearch_handler.setLevel(logging.INFO)
    gridsearch_formatter = logging.Formatter(
        "%(levelname)s - %(name)s -    %(message)s",
        datefmt="%y/%m/%d %H:%M:%S")
    gridsearch_handler.setFormatter(gridsearch_formatter)
    gridsearch_logger.addHandler(gridsearch_handler)

    def unroll(key, values):
        return ((key, value) for value in values)
    grid_search_args = list(itertools.product(*itertools.starmap(unroll, grid_values.items())))
    for i, grid_args in enumerate(grid_search_args):
        run_args = copy(args)
        run_args.exp_name += f'_{i}'
        for key, arg in grid_args:
            setattr(run_args, key, arg)
        gridsearch_logger.info(
            f"Running gridsearch {i} / {len(grid_search_args)} with args {grid_args}")
        run_train_distributed(run_args)
        args.master_addr += 1


if __name__ == '__main__':
    run_train_distributed()
