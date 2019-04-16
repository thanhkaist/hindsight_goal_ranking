import os
import sys
import click
import numpy as np
import json
from mpi4py import MPI
import resource

from baselines import logger
from baselines.common import set_global_seeds
from baselines.common.mpi_moments import mpi_moments
import config
from rollout import RolloutWorker, RolloutWorkerOriginal
from util import mpi_fork

from subprocess import CalledProcessError
from tqdm import tqdm
from tensorboardX import SummaryWriter

import sys
sys.path.append('../../')
from utils import tensorboard_log


def mpi_average(value):
    if value == []:
        value = [0.]
    if not isinstance(value, list):
        value = [value]
    return mpi_moments(np.array(value))[0]


def train(policy, rollout_worker, evaluator,
          n_epochs, n_test_rollouts, n_cycles, n_batches, policy_save_interval,
          save_policies, demo_file, tensorboard, **kwargs):
    rank = MPI.COMM_WORLD.Get_rank()

    latest_policy_path = os.path.join(logger.get_dir(), 'policy_latest.pkl')
    best_policy_path = os.path.join(logger.get_dir(), 'policy_best.pkl')
    periodic_policy_path = os.path.join(logger.get_dir(), 'policy_{}.pkl')

    logger.info("Training...")
    best_success_rate = -1
    best_success_epoch = 0

    if policy.bc_loss == 1:
        print('Initializing demonstration buffer...')
        policy.initDemoBuffer(demo_file)  #initializwe demo buffer
        # policy.initDemoBuffer(demo_file, load_from_pickle=True, pickle_file='demo_pickandplace.pkl')

    for epoch in tqdm(range(n_epochs)):
        # train
        rollout_worker.clear_history()
        for _ in range(n_cycles):
            episode = rollout_worker.generate_rollouts()
            policy.store_episode(episode)
            for _ in range(n_batches):
                policy.train()
            policy.update_target_net()

        # test
        logger.info("Testing")
        evaluator.clear_history()
        for _ in range(n_test_rollouts):
            evaluator.generate_rollouts()

        # record logs
        logger.record_tabular('epoch', epoch)

        log_dict = {}
        for key, val in evaluator.logs('test'):
            logger.record_tabular(key, mpi_average(val))
            log_dict[key] = mpi_average(val)
        for key, val in rollout_worker.logs('train'):
            logger.record_tabular(key, mpi_average(val))
            log_dict[key] = mpi_average(val)
        for key, val in policy.logs():
            logger.record_tabular(key, mpi_average(val))
            log_dict[key] = mpi_average(val)

        if rank == 0:
            logger.dump_tabular()

        # tensorboard_log(tensorboard, log_dict, epoch)
        tensorboard_log(tensorboard, log_dict, policy.buffer.n_transitions_stored)

        # save the policy if it's better than the previous ones
        success_rate = mpi_average(evaluator.current_success_rate())
        if rank == 0 and success_rate >= best_success_rate and save_policies:
            best_success_rate = success_rate
            best_success_epoch = epoch
            logger.info('New best success rate: {}. Saving policy to {} ...'.format(best_success_rate, best_policy_path))
            evaluator.save_policy(best_policy_path)
            evaluator.save_policy(latest_policy_path)
        if rank == 0 and policy_save_interval > 0 and epoch % policy_save_interval == 0 and save_policies:
            policy_path = periodic_policy_path.format(epoch)
            logger.info('Saving periodic policy to {} ...'.format(policy_path))
            evaluator.save_policy(policy_path)

        # make sure that different threads have different seeds
        logger.info("Best success rate so far ", best_success_rate, " In epoch number ", best_success_epoch)
        local_uniform = np.random.uniform(size=(1,))
        root_uniform = local_uniform.copy()
        MPI.COMM_WORLD.Bcast(root_uniform, root=0)
        if rank != 0:
            assert local_uniform[0] != root_uniform[0]


def launch(env, logdir, n_epochs, num_cpu, seed, replay_strategy, policy_save_interval,
           clip_return, demo_file=None, override_params={}, save_policies=True):

    tensorboard = SummaryWriter(logdir)
    # Fork for multi-CPU MPI implementation.
    if num_cpu > 1:
        try:
            whoami = mpi_fork(num_cpu, ['--bind-to', 'core'])
        except CalledProcessError:
            # fancy version of mpi call failed, try simple version
            whoami = mpi_fork(num_cpu)

        if whoami == 'parent':
            sys.exit(0)
        import baselines.common.tf_util as U
        U.single_threaded_session().__enter__()
    rank = MPI.COMM_WORLD.Get_rank()

    # Configure logging
    if rank == 0:
        if logdir or logger.get_dir() is None:
            logger.configure(dir=logdir)
    else:
        logger.configure()
    logdir = logger.get_dir()
    assert logdir is not None
    os.makedirs(logdir, exist_ok=True)

    # Seed everything.
    rank_seed = seed + 1000000 * rank
    set_global_seeds(rank_seed)
    resource.setrlimit(resource.RLIMIT_NOFILE, (65536, 65536))

    # Prepare params.
    params = config.DEFAULT_PARAMS
    params['env_name'] = env
    params['replay_strategy'] = replay_strategy
    if env in config.DEFAULT_ENV_PARAMS:
        params.update(config.DEFAULT_ENV_PARAMS[env])  # merge env-specific parameters in
    params.update(**override_params)  # makes it possible to override any parameter

    with open(os.path.join(logger.get_dir(), 'params.json'), 'w') as f:
        json.dump(params, f)
    params = config.prepare_params(params)
    config.log_params(params, logger=logger)

    dims = config.configure_dims(params)
    policy = config.configure_ddpg(dims=dims, params=params, clip_return=clip_return)

    rollout_params = {
        'exploit': False,
        'use_target_net': False,
        # 'use_demo_states': True,
        'compute_Q': False,
        'T': params['T'],
        #'render': 1,
    }

    eval_params = {
        'exploit': True,
        'use_target_net': params['test_with_polyak'],
        #'use_demo_states': False,
        'compute_Q': True,
        'T': params['T'],
        #'render': 1,
    }

    for name in ['T', 'rollout_batch_size', 'gamma', 'noise_eps', 'random_eps']:
        rollout_params[name] = params[name]
        eval_params[name] = params[name]

    rollout_worker = RolloutWorkerOriginal(params['make_env'], policy, dims, logger, **rollout_params)
    rollout_worker.seed(rank_seed)

    evaluator = RolloutWorkerOriginal(params['make_env'], policy, dims, logger, **eval_params)
    evaluator.seed(rank_seed)

    train(logdir=logdir, policy=policy, rollout_worker=rollout_worker,
          evaluator=evaluator, n_epochs=n_epochs, n_test_rollouts=params['n_test_rollouts'],
          n_cycles=params['n_cycles'], n_batches=params['n_batches'],
          policy_save_interval=policy_save_interval, save_policies=save_policies, demo_file=demo_file,
          tensorboard=tensorboard)


@click.command()
@click.option('--env', type=str, default='FetchPickAndPlace-v1', help='the name of the OpenAI Gym environment')
@click.option('--logdir', type=str, default='../../logs/ddpg_her_use_bc_no_qfil_2')
@click.option('--n_epochs', type=int, default=200, help='the number of training epochs to run')
@click.option('--num_cpu', type=int, default=1, help='the number of CPU cores to use (using MPI)')
@click.option('--seed', type=int, default=0, help='random seed used for both the environment and the training code')
@click.option('--policy_save_interval', type=int, default=10, help='the interval with which policy pickles are saved. If set to 0, only the best and latest policy will be pickled.')
@click.option('--replay_strategy', type=click.Choice(['future', 'none']), default='future', help='the HER replay strategy to be used. "future" uses HER, "none" disables HER.')
@click.option('--clip_return', type=int, default=1, help='whether or not returns should be clipped')
@click.option('--demo_file', type=str, default='../data_generation/demonstration_FetchPickAndPlace_100_best.npz', help='demo data file path')
def main(**kwargs):
    kwargs['logdir'] = kwargs['logdir'] + '_' + kwargs['env'].split('-')[0]
    launch(**kwargs)


if __name__ == '__main__':
    main()
