import os
import sys
import subprocess
sys.path.append(os.path.dirname(os.path.realpath(__file__)) + "/../")
import utils
import run_utils

def get_tvm_target(arg):
    if arg == "cuda": return "cuda"
    elif arg == "cpu" or arg == "llvm": return "llvm -mcpu=cascadelake"
    elif arg == "arm": return "llvm -mcpu=cortex-a76 -mattr=neon"

def run_cmd(cmd):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.decode('utf-8'), result.stderr.decode('utf-8')

def get_all_datasets():
    return list(run_utils.dataset_max_lens.keys())

def get_dataset_file(dataset):
    return run_utils.DATA_DIR + run_utils.dataset_files[dataset]

def cluster_datasets_by_max_len():
    ret = {}
    for ds, ml in run_utils.dataset_max_lens.items():
        if ml in ret: ret[ml].append(ds)
        else: ret[ml] = [ds]
    return ret

def get_dataset_max_len(dataset):
    return run_utils.get_dataset_max_len(dataset)

marker = 'RESULTS'
mem_marker = 'MEM'
INF = 100000000
def extract_times(out, expect_num):
    lines = out.splitlines()
    res_line = None
    for line in lines:
        if marker in line:
            res_line = line
            break
    if res_line:
        arr = res_line.split(',')
        assert len(arr) == expect_num + 1
        return [float(i) for i in arr[1:]]
    else:
        return [INF] * expect_num

def extract_mem(out):
    lines = out.splitlines()
    res_line = None
    for line in lines:
        if mem_marker in line:
            res_line = line
            break
    if res_line:
        arr = res_line.split(',')
        return float(arr[1])
    else:
        return INF

def batchify(b_sizes, fun, *args):
    result = {}
    for b_size in b_sizes: result[b_size] = fun(b_size, *args)
    return result

def extract_times_multiple(out):
    lines = out.splitlines()
    res_lines = []
    for line in lines:
        if marker in line:
            res_lines.append(line)

    res = []
    for line in res_lines:
        arr = line.split(',')
        res.append((float(arr[1]), float(arr[2])))

    return res

def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)

def get_out_files(args, prefix, mode = 'w'):
    out_dir = os.getcwd() + '/' + args.out_dir
    ensure_dir(out_dir)
    results_out = sys.stdout if args.stdout else open(out_dir + '/' + prefix + '_results_' + args.target + '.csv', mode)
    results_err = sys.stderr if args.stdout else open(out_dir + '/' + prefix + '_errors_' + args.target + '.txt', mode)
    return results_out, results_err


def log(args, string):
    if not args.stdout:
        print(string)


SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
LINEARIZATION_BIN = SCRIPT_DIR + '/../benchmark_runners/bin/linearization'
def run_linearization(err_file, dataset, b_size, n_batch, *args):
    cmd = [LINEARIZATION_BIN, str(b_size), str(n_batch), dataset] + [str(arg) for arg in args]
    print(' '.join(cmd))
    out, err = run_cmd(cmd)
    if err:
        print(err, file = err_file)
        out = 'ERROR'

    return extract_times(out)
