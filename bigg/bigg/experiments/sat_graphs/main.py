# coding=utf-8
# Copyright 2023 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
# pylint: skip-file

import os
import sys
import pickle as cp
import networkx as nx
import numpy as np
import random
from tqdm import tqdm
import torch
import torch.optim as optim

from bigg.common.configs import cmd_args, set_device
from bigg.model.tree_model import RecurTreeGen
from bigg.model.tree_clib.tree_lib import setup_treelib, TreeLib
from bigg.experiments.train_utils import sqrtn_forward_backward


def load_graphs(graph_pkl, stats_pkl):
    graphs = []
    with open(graph_pkl, 'rb') as f:
        while True:
            try:
                g = cp.load(f)
            except:
                break
            graphs.append(g)
    with open(stats_pkl, 'rb') as f:
        stats = cp.load(f)
    assert len(graphs) == len(stats)

    for g, stat in zip(graphs, stats):
        n, m = stat
        TreeLib.InsertGraph(g, bipart_stats=(n, m))
    return graphs, stats


def LCG_to_sat(graph, save_name, num_var):
    nodes = list(graph.nodes())
    clauses = []
    for node in nodes:
        if (node >= num_var * 2):
            neighbors = list(graph.neighbors(node))
            if len(neighbors) == 0:
                continue
            clause = ""
            for lit in neighbors:
                if lit < num_var:
                    clause += "{} ".format(lit + 1)
                else:
                    assert(lit < 2 * num_var)
                    clause += "{} ".format(-(lit - num_var + 1))
            clause += "0\n"
            clauses.append(clause)
    with open(save_name, 'w') as out_file:
        out_file.write("c generated by G2SAT lcg\n")
        out_file.write("p cnf {} {}\n".format(num_var, len(clauses)))
        for clause in clauses:
            out_file.write(clause)

def get_ordered_edges(g, offset):
    true_edges = []
    for e in g.edges():
        if e[0] > e[1]:
            e = (e[1], e[0])
        true_edges.append((e[0], e[1] - offset))
    true_edges.sort(key=lambda  x: x[0])
    return true_edges


if __name__ == '__main__':
    random.seed(cmd_args.seed)
    torch.manual_seed(cmd_args.seed)
    np.random.seed(cmd_args.seed)
    set_device(cmd_args.gpu)
    setup_treelib(cmd_args)

    train_graphs, stats = load_graphs(os.path.join(cmd_args.data_dir, 'train-graphs.pkl'),
                                      os.path.join(cmd_args.data_dir, 'train-graph-stats.pkl'))
    max_left = 0
    max_right = 0
    for n, m in stats:
        max_left = max(max_left, n)
        max_right = max(max_right, m)
    max_num_nodes = max(max_left, max_right)
    print('max # nodes:', max_num_nodes, 'max_left:', max_left, 'max_right:', max_right)
    cmd_args.max_num_nodes = max_num_nodes

    model = RecurTreeGen(cmd_args).to(cmd_args.device)
    if cmd_args.model_dump is not None and os.path.isfile(cmd_args.model_dump):
        print('loading from', cmd_args.model_dump)
        model.load_state_dict(torch.load(cmd_args.model_dump))

    if cmd_args.eval_folder is not None:
        g_idx = 0
        eval_dir = os.path.join(cmd_args.data_dir, '../sat-%s' % cmd_args.eval_folder)
        print('loading eval from', eval_dir)
        test_graphs, stats = load_graphs(os.path.join(eval_dir, 'test-graphs.pkl'),
                                        os.path.join(eval_dir, 'test-graph-stats.pkl'))
        out_dir = os.path.join(cmd_args.save_dir, '%s-pred_formulas-e-%d-g-%.2f' % (cmd_args.eval_folder, cmd_args.epoch_load, cmd_args.greedy_frac))
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        for _ in range(1):
            for g, stat in tqdm(zip(test_graphs, stats)):
                degree_list = [g.degree(i) for i in range(stat[0])]
                with torch.no_grad():
                    _, pred_edges, _ = model(stat[0], lb_list=degree_list, ub_list=degree_list, col_range=(0, stat[1]), num_nodes=len(g), display=cmd_args.display)
                    pred_edges = [(e[0], e[1] + stat[0]) for e in pred_edges]
                    true_edges = get_ordered_edges(g, offset=0)
                    common = set(pred_edges).intersection(set(true_edges))
                    print(len(pred_edges), len(true_edges), 'precision', len(common) / len(pred_edges), 'recall', len(common) / len(true_edges))
                    out_file = os.path.join(out_dir, 'gen_%d.cnf' % g_idx)
                    pred_g = nx.Graph()
                    pred_g.add_edges_from(pred_edges)
                    LCG_to_sat(pred_g, out_file, stat[0] // 2)
                    g_idx += 1
        sys.exit()


    optimizer = optim.Adam(model.parameters(), lr=cmd_args.learning_rate, weight_decay=1e-4)
    indices = list(range(len(train_graphs)))
    for epoch in range(cmd_args.num_epochs):
        pbar = tqdm(range(cmd_args.epoch_save))

        optimizer.zero_grad()
        for idx in pbar:
            random.shuffle(indices)
            batch_indices = indices[:cmd_args.batch_size]

            num_nodes = sum([len(train_graphs[i]) for i in batch_indices])
            if cmd_args.blksize< 0 or num_nodes <= cmd_args.blksize:
                ll, _ = model.forward_train(batch_indices, list_col_ranges=[(0, stats[i][1]) for i in batch_indices])
                loss = -ll / num_nodes
                loss.backward()
                loss = loss.item()
            else:
                ll = 0.0
                for i in batch_indices:
                    n = len(train_graphs[i])
                    cur_ll, _ = sqrtn_forward_backward(model, graph_ids=[i], list_node_starts=[0],
                                                    num_nodes=stats[i][0], blksize=cmd_args.blksize, loss_scale=1.0/n, list_col_ranges=[(0, stats[i][1])])
                    ll += cur_ll
                loss = -ll / num_nodes

            if False:
                true_edges = get_ordered_edges(train_graphs[0], offset=stats[0][0])
                #ll2, _, _ = model(stats[0][0], edge_list=true_edges, col_range=(0, stats[0][1]))
                print(-ll / num_nodes)
                #print(ll2)
                sys.exit()
            if (idx + 1) % cmd_args.accum_grad == 0:
                if cmd_args.grad_clip > 0:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=cmd_args.grad_clip)
                optimizer.step()
                optimizer.zero_grad()
            pbar.set_description('epoch %.2f, loss: %.4f' % (epoch + (idx + 1) / cmd_args.epoch_save, loss))


        torch.save(model.state_dict(), os.path.join(cmd_args.save_dir, 'epoch-%d.ckpt' % epoch))
