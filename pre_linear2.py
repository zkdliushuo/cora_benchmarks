import math
import os
import utils
import run_utils
import argparse
import tvm
from tvm import tir, te
from tvm.te import RangeDimension as Dim
from tvm.tir import UninterpFun as Uf

parser = argparse.ArgumentParser()
parser.add_argument('--target', nargs='?', default='llvm')
parser.add_argument('--dtype', dest='dtype', nargs='?', default='float32')
parser.add_argument('--max-batches', dest='max_batches', default=10, type=int)
parser.add_argument('--batch-size', dest='batch_size', default=32, type=int)
parser.add_argument('--peel-loops', dest='peel_loops', default=False, action='store_true')
parser.add_argument('--unroll-loops', dest='unroll_loops', default=False, action='store_true')
parser.add_argument('--debug', dest='debug', default=False, action='store_true')
parser.add_argument('--debug-code', dest='debug_code', default=False, action='store_true')
parser.add_argument('--manual-code', dest='manual_code', default=False, action='store_true')
parser.add_argument('--dense-storage', dest='dense_storage', default=False, action='store_true')
parser.add_argument('--dataset', nargs='?', default='random')
parser.add_argument('--datadir', nargs='?', default='random')
args = parser.parse_args()

NUM_HEADS = 8
IN_SIZE = 512
OUT_SIZE = 64
QKV_NUM = 3
MAX_LEN = utils.ceilmult(run_utils.get_dataset_max_len(args.dataset), 64)

lens = te.placeholder((args.batch_size,), name = 'lens', dtype = 'int32')

bd = Dim('bd')
qkv = Dim('qkv')
md = Dim('md')
s1 = Dim('s1')
id = Dim('id')
od = Dim('od')

def len_uf(name, padding=1): return Uf(name, "l", (padding, MAX_LEN), [bd], lambda b: utils.ceilmult(lens[b], padding))

ls =  {
    0: Uf.from_constant('qkv', QKV_NUM, "l"),
    1: Uf.from_constant('bd', args.batch_size, "l"),
    2: Uf.from_constant('md', NUM_HEADS, "l"),
    3: len_uf('s1', 1),
    4: Uf.from_constant('id', IN_SIZE, "l"),
    5: Uf.from_constant('od', OUT_SIZE, "l"),
}

loop_ufs=[ls[1], ls[3], ls[4]]
width_ufs=loop_ufs
QKV = te.ragged_placeholder((args.batch_size, MAX_LEN, IN_SIZE), [bd, s1, id], loop_ufs,
                            name='QKV', width_ufs=width_ufs)

W = te.placeholder((QKV_NUM, NUM_HEADS, OUT_SIZE, IN_SIZE), name='W')

loop_ufs=[ls[0], ls[1], ls[3], ls[2], ls[5]]
width_ufs=[[ls[0], ls[1], len_uf('s2', 64), ls[2], ls[5]]]
k = tvm.reduce_axis((0, IN_SIZE), name = 'k')
O = te.ragged_compute((QKV_NUM, args.batch_size, MAX_LEN, NUM_HEADS, OUT_SIZE), [qkv, bd, s1, md, od], loop_ufs,
                      lambda ds: tvm.sum(W[ds[qkv], ds[md], ds[od], k] * QKV[ds[bd], ds[s1], k],
                                         axis = k, dimensions = [id]),
                      name = 'O', width_uf_lists=width_ufs)

s = tvm.create_schedule([O.op])

if args.target == "cuda":
    s.fuse_tensor_dimensions(QKV, 0, 1)

    O_local, = s.cache_write([O], "local")
    s.fuse_tensor_dimensions(O_local, 1, 2)
    q_c, b_c, l_c, n_c, h_c, k = tuple(O_local.op.axis) + tuple(O_local.op.reduce_axis)
    l_c = s[O_local].fuse(b_c, l_c)
    l_coi, l_ci = s[O_local].split(l_c, factor=2)
    koo, koi = s[O_local].split(k, factor=8)
    s[O_local].reorder(q_c, h_c, koo, koi, l_coi, l_ci, n_c)
    s[O_local].mark_no_bounds_check()

    O_q, O_b, O_l, O_n, O_h, O_k = tuple(O.op.axis) + tuple(O.op.reduce_axis)
    O_l = s[O].fuse(O_b, O_l, padding = 64)

    O_loi, O_li = s[O].split(O_l, factor=8)
    O_looi, O_loi = s[O].split(O_loi, factor=4)
    O_looo, O_looi = s[O].split(O_looi, factor=2)

    O_noi, O_ni = s[O].split(O_n, factor=2)
    O_nooi, O_noi = s[O].split(O_noi, factor=2)

    O_hooi, O_hoi = s[O].split(O_h, factor=8)
    O_hooo, O_hooi = s[O].split(O_hooi, factor=2)

    s[O].reorder(O_q, O_looo, O_nooi, O_hooo, O_looi, O_hooi, O_loi, O_noi, O_hoi, O_li, O_ni)

    QKV_sh = s.cache_read(QKV, "shared", [O_local])
    s.fuse_tensor_dimensions(QKV_sh, 0, 1)
    QKV_sh_ax00, QKV_sh_ax01, QKV_sh_ax1 = tuple(QKV_sh.op.axis)
    QKV_sh_ax0 = s[QKV_sh].fuse(QKV_sh_ax00, QKV_sh_ax01)
    s[QKV_sh].compute_at(s[O_local], koo)
    s[QKV_sh].mark_no_bounds_check()

    W_sh = s.cache_read(W, "shared", [O_local], vanilla = True)
    W_sh_ax0, W_sh_ax1, W_sh_ax2, W_sh_ax3 = tuple(W_sh.op.axis)
    s[W_sh].compute_at(s[O_local], koo)

    s[O].bind(O_q, te.thread_axis("blockIdx.z"))
    s[O].bind(O_looo, te.thread_axis("blockIdx.y"))
    O_q_looo_f_nooo_f_hooo_f = s[O].fuse(O_nooi, O_hooo)
    s[O].bind(O_q_looo_f_nooo_f_hooo_f, te.thread_axis("blockIdx.x"))
    O_qooi_looi_f_nooi_f_hooi_f = s[O].fuse(O_looi, O_hooi)
    s[O].bind(O_qooi_looi_f_nooi_f_hooi_f, te.thread_axis("vthread"), no_unroll_vthread=args.debug_code)
    O_qoi_loi_f_noi_f_hoi_f = s[O].fuse(O_loi, O_noi, O_hoi)
    s[O].bind(O_qoi_loi_f_noi_f_hoi_f, te.thread_axis("threadIdx.x"))
    s[O_local].compute_at(s[O], O_qoi_loi_f_noi_f_hoi_f)

    QKV_sh_ax0_ax1_f = s[QKV_sh].fuse(QKV_sh_ax0, QKV_sh_ax1)
    QKV_sh_ax0_ax1_f_o, QKV_sh_ax0_ax1_f_i = s[QKV_sh].split(QKV_sh_ax0_ax1_f, factor=4)
    s[QKV_sh].vectorize(QKV_sh_ax0_ax1_f_i)
    QKV_sh_ax0_ax1_f_o_o, QKV_sh_ax0_ax1_f_o_i = s[QKV_sh].split(QKV_sh_ax0_ax1_f_o, factor=64)
    s[QKV_sh].bind(QKV_sh_ax0_ax1_f_o_i, te.thread_axis("threadIdx.x"))

    W_sh_ax0_ax1_f_ax2_f_ax3_f = s[W_sh].fuse(W_sh_ax0, W_sh_ax1, W_sh_ax2, W_sh_ax3)
    W_sh_ax0_ax1_f_ax2_f_ax3_f_o, W_sh_ax0_ax1_f_ax2_f_ax3_f_i = s[W_sh].split(W_sh_ax0_ax1_f_ax2_f_ax3_f, factor=4)
    s[W_sh].vectorize(W_sh_ax0_ax1_f_ax2_f_ax3_f_i)
    W_sh_ax0_ax1_f_ax2_f_ax3_f_o_o, W_sh_ax0_ax1_f_ax2_f_ax3_f_o_i = s[W_sh].split(W_sh_ax0_ax1_f_ax2_f_ax3_f_o, factor=64)
    s[W_sh].bind(W_sh_ax0_ax1_f_ax2_f_ax3_f_o_i, te.thread_axis("threadIdx.x"))

    if not args.debug_code:
        s[O_local].pragma(q_c, "auto_unroll_max_step", 512)
        s[O_local].pragma(q_c, "unroll_explicit", True)


    gen_prefix = os.path.splitext(os.path.basename(os.path.realpath(__file__)))[0]
    _ = tvm.register_func(utils.get_tvm_callback_cuda_compile(256))
    _ = tvm.register_func(
        utils.get_tvm_callback_cuda_postproc(args, os.path.realpath(__file__), fileprefix=gen_prefix))
else:
    s.reorder_tensor_dimensions(O, 1, 2)
    s.fuse_tensor_dimensions(O, 2, 3)
    s.fuse_tensor_dimensions(QKV, 1, 2)

bQKV = tvm.decl_buffer([args.batch_size*MAX_LEN, IN_SIZE], name = "bQKV")
inputs = [[lens], [bQKV, W, O]]
with tvm.build_config(prep_code_mode='with_prep_code', fill_in_function_bodies=True):
    if args.debug_code:
        lowered = tvm.lower(s, inputs, args.target, simple_mode = True, binds = {QKV: bQKV})
        print(lowered)
        # fadd, _ = tvm.build(s, inputs, args.target, binds = {QKV: bQKV})
        # if args.target == 'cuda':
            # print('-----GPU code-----\n' + fadd.imported_modules[0].get_source())
        # else:
            # print('-----CPU code-----\n' + fadd.get_source())
    else:
        fadd, i_bufs = tvm.build(s, inputs, args.target, binds = {QKV: bQKV})
        # fadd = tvm.runtime.module.load_module('/home/ppf/rnn_compilers/ragged_tensors/incubator-tvm/build/qkt.so')
        run_utils.run(fadd, i_bufs, inputs[1], args.batch_size, args.max_batches,
                      args.dataset, args.datadir, args.target, args.debug)
