import os
import sys
import common as com
from common import run_cmd, INF, get_out_files, log, run_linearization
import argparse

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CBT_RUNNER = SCRIPT_DIR + '/../vbatch_gemm/cbt/gemm'
CUBLAS_RUNNER = SCRIPT_DIR + '/../vbatch_gemm/cublas/gemm_cublas'
MKL_RUNNER = SCRIPT_DIR + '/../vbatch_gemm/mkl/vbatch_gemm'
TVM_RUNNER = SCRIPT_DIR + '/../vbatch_gemm/tvm/vbatch_gemm.py'
TVM_MKL_RUNNER = SCRIPT_DIR + '/../vbatch_gemm/tvm/cpu_mklized.py'
DATA_FILE_PATH = SCRIPT_DIR + '/../vbatch_gemm/data.txt'
PYTHON = 'python3'

def get_mkl_runner(pad):
    pad_arg = '1' if pad else '0'
    def run_mkl(b_size, n_batch, data_file_path, err_file, args):
        cmd = [MKL_RUNNER, str(b_size), str(n_batch), pad_arg, data_file_path, str(100), str(1)]
        out, err = run_cmd(cmd)
        if err: print(err, file = err_file)
        return com.extract_times(out, 1)[0]
    return run_mkl

def run_cbt(b_size, n_batch, data_file_path, err_file, args):
    cmd = [CBT_RUNNER, str(b_size), str(n_batch), data_file_path, str(100), str(1), 'gemm']
    # print(' '.join(cmd))
    out, err = run_cmd(cmd)
    if err: print(err, file = err_file)
    return com.extract_times(out, 1)[0]

def run_cublas(b_size, n_batch, data_file_path, err_file, args):
    cmd = [CUBLAS_RUNNER, str(b_size), str(n_batch), data_file_path, 'nn', str(100), str(1)]
    out, err = run_cmd(cmd)
    if err: print(err, file = err_file)
    return com.extract_times(out, 1)[0]

def get_tvm_runner(mklized):
    def run_tvm(b_sizes, n_batch, data_file_path, err_file, args):
        runner = TVM_MKL_RUNNER if mklized else TVM_RUNNER

        cmd = ([PYTHON, runner, '--target', com.get_tvm_target(target), '--batch-sizes'] +
               [str(i) for i in b_sizes] +
               ['--max-batches', str(n_batch), '--data-file', data_file_path])
        if args.prep_overhead:
            cmd += ['--only-prep-code']
        out, err = run_cmd(cmd)
        if err: print(err, file = err_file)

        return com.extract_time_batches(out)
    return run_tvm

parser = argparse.ArgumentParser()
parser.add_argument('--target', nargs='?', default=None)
parser.add_argument('--out-dir', dest='out_dir', nargs='?', default='perf_results')
parser.add_argument('--max-batches', dest='max_batches', default=1, type=int)
parser.add_argument('--prep-overhead', dest='prep_overhead', default=False, action='store_true')
parser.add_argument('--stdout', dest='stdout', default=False, action='store_true')
parser.add_argument('--append', dest='append', default=False, action='store_true')
args = parser.parse_args()

batch_sizes = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]
# batch_sizes = [256, 512]
targets = [args.target] if args.target else ['cuda']

if args.prep_overhead:
    framework_funs = { 'cora': run_tvm }
else:
    if args.target == 'cuda':
        framework_funs = {
            'cbt': lambda b_sizes, *args: com.batchify(run_cbt, *args),
            # 'cublas': lambda b_sizes, *args: com.batchify(run_cublas, *args),
            # 'cora': get_tvm_runner(False),
        }
    else:
        framework_funs = {
            # 'mkl_nopad': lambda b_sizes, *args: com.batchify(get_mkl_runner(False), *args),
            # 'mkl_pad': lambda b_sizes, *args: com.batchify(get_mkl_runner(True), *args),
            # 'cora': get_tvm_runner(False),
            'cora_mkl': get_tvm_runner(True),
        }

results_out, results_err = get_out_files(args, 'vbatch_gemm', 'a' if args.append else 'w')
header = 'Target,Batch Size'
for framework, func in framework_funs.items(): header += ',' + framework + ' (ms)'
print(header, file = results_out)

for target in targets:
    exe_times = {}
    for framework, func in framework_funs.items():
        log(args, 'Running %s %s' % (target, framework))
        exe_times[framework] = func(batch_sizes, args.max_batches, DATA_FILE_PATH, results_err, args)
        print(exe_times[framework])

    for b_size in batch_sizes:
        out_str = '%s,%d' % (target, b_size)
        for framework, framework_exe_time in exe_times.items():
            out_str += ',%g' % framework_exe_time[b_size]
        print(out_str, file = results_out)

if not args.stdout:
    results_out.close()
    results_err.close()
