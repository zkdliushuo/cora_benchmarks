import os
import argparse
import numpy as np
import time
import torch
import torch.nn.functional as f
# from torch.profiler import profile, record_function, ProfilerActivity
from torch import Tensor
from torch import nn
import torch.utils.benchmark as benchmark
import sys
sys.path.append(os.path.dirname(os.path.realpath(__file__)) + "/../../")
import run_utils
import utils

parser = argparse.ArgumentParser()
parser.add_argument('--target', nargs='?', default='llvm')
parser.add_argument('--dtype', dest='dtype', nargs='?', default='float32')
parser.add_argument('--max-batches', dest='max_batches', default=10, type=int)
parser.add_argument('--batch-size', dest='batch_size', default=32, type=int)
parser.add_argument('--profile', dest='profile', default=False, action='store_true')
parser.add_argument('--mem', dest='mem', default=False, action='store_true')
parser.add_argument('--masked-mha', dest='masked_mha', default=False, action='store_true')
parser.add_argument('--debug', dest='debug', default=False, action='store_true')
parser.add_argument('--no-ub', dest='no_ub', default=False, action='store_true')
parser.add_argument('--dataset', nargs='?', default='random_384_512')
args = parser.parse_args()

np.random.seed(0)
VAL=0.1
def get_np_tensor(size, device, random, fill_value = None):
    if random:
        np_array = np.random.normal(size=size).astype('float32')
        return torch.randn(size, device = device, requires_grad = False, dtype = torch.float32)
    else:
        if fill_value == None: raise ValueError("No fill value provided " + str(fill_value))
        np_array = np.full(size, 0.1, 'float32').astype('float32')
    return torch.from_numpy(np_array).to(device)

def mean(l): return sum(l) / len(l)

class Encoder(nn.Module):
    def __init__(self, device, max_len, batch_size, num_heads, head_size, model_size, ff_size, debug):
        super(Encoder, self).__init__()
        self.pre_linear_w = get_np_tensor((3, num_heads, model_size, head_size), device, not debug, VAL)
        self.pre_linear_b = get_np_tensor((3, num_heads, 1, head_size), device, not debug, VAL)
        self.post_linear_w = get_np_tensor((model_size, model_size), device, not debug, VAL)
        self.post_linear_b = get_np_tensor((model_size,), device, not debug, VAL)
        self.ff1_w = get_np_tensor((model_size, ff_size), device, not debug, VAL)
        self.ff2_w = get_np_tensor((ff_size, model_size), device, not debug, VAL)
        self.ff1_b = get_np_tensor((ff_size,), device, not debug, VAL)
        self.ff2_b = get_np_tensor((model_size,), device, not debug, VAL)
        self.batch_size = batch_size
        self.num_heads = num_heads
        self.head_size = head_size
        self.model_size = model_size
        self.ff_size = ff_size
        self.max_len = max_len
        self.layer_norm1 = torch.nn.LayerNorm((self.model_size,), elementwise_affine=not debug, device=device)
        self.layer_norm2 = torch.nn.LayerNorm((self.model_size,), elementwise_affine=not debug, device=device)

    def forward(self, inp, attn_mask):
        qkv = torch.matmul(inp, self.pre_linear_w)
        qkv += self.pre_linear_b
        qkv = qkv.view(3, self.num_heads, self.batch_size, self.max_len, self.head_size)
        q, k, v = torch.split(qkv, 1, 0)
        attn = torch.matmul(q, k.permute(0, 1, 2, 4, 3))
        attn += attn_mask
        attn = f.softmax(attn, dim = 4)
        attn = torch.reshape(torch.matmul(attn, v).permute(0, 2, 3, 1, 4), (self.batch_size, self.max_len, self.model_size))
        sa_out = torch.matmul(attn, self.post_linear_w)
        sa_out += self.post_linear_b
        sa_out += inp.view(self.batch_size, self.max_len, self.model_size)
        sa_out = self.layer_norm1(sa_out)

        ff1_out = torch.matmul(sa_out, self.ff1_w)
        ff1_out += self.ff1_b
        ff1_out = f.gelu(ff1_out)
        ff2_out = torch.matmul(ff1_out, self.ff2_w)
        ff2_out += self.ff2_b
        ff2_out += sa_out
        ff_out = self.layer_norm2(ff2_out)
        return ff_out

class MaskedMHA(nn.Module):
    def __init__(self, device, max_len, batch_size, num_heads, head_size, model_size):
        super(MaskedMHA, self).__init__()
        self.pre_linear_w = get_np_tensor((3, num_heads, model_size, head_size), device, True)
        self.pre_linear_b = get_np_tensor((3, num_heads, 1, head_size), device, True)
        self.post_linear_w = get_np_tensor((model_size, model_size), device, True, VAL)
        self.post_linear_b = get_np_tensor((model_size,), device, True, VAL)
        self.batch_size = batch_size
        self.num_heads = num_heads
        self.head_size = head_size
        self.model_size = model_size
        self.max_len = max_len
        self.qkt = get_np_tensor((1, num_heads, batch_size, max_len, max_len), device, True, VAL)


    def forward(self, inp, attn_mask):
        qkv = torch.matmul(inp, self.pre_linear_w)
        qkv += self.pre_linear_b
        qkv = qkv.view(3, self.num_heads, self.batch_size, self.max_len, self.head_size)
        q, k, v = torch.split(qkv, 1, 0)
        attn = torch.matmul(q, torch.clone(k.permute(0, 1, 2, 4, 3)))
        attn += attn_mask
        attn = f.softmax(attn, dim = 4)
        attn = torch.reshape(torch.clone(torch.matmul(attn, v).permute(0, 2, 3, 1, 4)),
                             (self.batch_size, self.max_len, self.model_size))
        sa_out = torch.matmul(attn, self.post_linear_w)
        sa_out += self.post_linear_b
        return sa_out

num_heads = 8
head_size = 64
ff_size = 2048
model_size = num_heads * head_size
device = torch.device('cpu')
batch_size = args.batch_size

batches = run_utils.get_nlp_batches(batch_size, args.max_batches, args.dataset)
if len(batches[-1]) != batch_size: batches.pop()

torch.set_num_threads(64)

def run_for_a_batch(batch, iters):
    batch_size = len(batch)
    max_len = int(np.amax(batch))
    print(batch_size, max_len, batch)
    attn_mask = np.full((batch_size, max_len, max_len), 0.0, dtype='float32')
    if args.masked_mha:
        # for i in range(batch_size):
            # for j in range(max_len):
                # if j >= batch[i]:
                    # attn_mask[i][j] = np.full((max_len,), -float('inf'), dtype='float32')
                # else:
                    # attn_mask[i][j][j+1:] = np.full((max_len - j - 1,), -float('inf'), dtype='float32')
        attn_mask = torch.from_numpy(attn_mask).to(device)
        encoder = MaskedMHA(device, max_len, batch_size, num_heads, head_size, model_size)
    else:
        # for i in range(batch_size):
        #     for j in range(max_len):
        #         if j >= batch[i]:
        #             attn_mask[i][j] = np.full((max_len,), -float('inf'), dtype='float32')
        #         else:
        #             attn_mask[i][j][j+1:] = np.full((max_len - j - 1,), -float('inf'), dtype='float32')
        attn_mask = torch.from_numpy(attn_mask).to(device)
        encoder = Encoder(device, max_len, batch_size, num_heads, head_size, model_size, ff_size, args.debug)

    if args.debug:
        inp = get_np_tensor((batch_size * max_len, model_size), device, True)
        ret = encoder.forward(inp, attn_mask)
        # print(np.mean(ret.cpu().numpy()))
        return 1
    else:
        traced_encoder = torch.jit.script(encoder)
        inp = get_np_tensor((batch_size * max_len, model_size), device, True)
        timer = benchmark.Timer(stmt='f(x, y)',
                                globals={'x': inp, 'y': attn_mask, 'f': traced_encoder},
                                num_threads=64)
        return timer.timeit(iters).mean * 1000.0

def run_for_batches(ubs, iters):
    batch_times = []
    for batch in batches:
        bs = batch_size
        batch = np.sort(batch)
        micro_batches = np.split(batch, bs // ubs)

        print(ubs, bs, len(micro_batches))
        batch_time = 0
        for micro_batch in micro_batches:
            batch_time += run_for_a_batch(micro_batch, iters)
        batch_times.append(batch_time)

    return sum(batch_times) / len(batches)

iters = 1 if args.mem or args.debug else 40
with torch.no_grad():
    if not args.profile:
        if args.no_ub:
            time = run_for_batches(batch_size, iters)
            print('RESULTS', time, batch_size, sep=',')
        else:
            min_time = float('inf')
            min_ubs = -1
            for ubs in [2, 4, 8, 16, 32, 64, 128]:
                if ubs > batch_size:
                    break
                ubs_time = run_for_batches(ubs, 5)
                print(ubs, ubs_time)
                if ubs_time < min_time:
                    min_time = ubs_time
                    min_ubs = ubs

            time = run_for_batches(min_ubs, iters)
            print('RESULTS', time, min_ubs, sep=',')
    else:
        with profile(activities=[ProfilerActivity.CUDA], record_shapes=True) as prof:
            run_for_batches()
            print(prof.key_averages(group_by_stack_n=5))

if args.mem:
    if args.target != "cuda": raise ValueError("Mem measurement only supported for GPUs")
    max_buffer_mem_alloced = torch.cuda.max_memory_allocated()
    print("MEM,%g" % (max_buffer_mem_alloced / (1024.0 * 1024.0)))
