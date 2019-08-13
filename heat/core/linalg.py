import itertools
import sys
import torch

from .communication import MPI
from . import dndarray
from . import factories
from . import manipulations
from . import types

__all__ = [
    'matmul',
    'qr',
    'transpose',
    'tril',
    'triu',
    'form_w',
    'larft'
]


def larft(v, tau, t=None):
    """
    forms the triangular factor T of a real block reflector H of order n, which is defined as a product of k elementary reflectors.
    This is a special case of the LAPACK subroutine of the same name for use with QR. (this function is not implemented in PyTorch

    Parameters
    ----------
    n : int
        order of the block reflector, H.
        must be >= 0
    k : int == nb
        order of the triangular factor T
        must be >= 1
    v : PyTorch Tensor, shape: m x nb
        array of the transform vectors, lower triangular, can be calculated by torch.geqrf
    tau : PyTorch Tensor
          array of scalar factors of the elementary reflector H_i, can be calculated by torch.geqrf
    t : optional, PyTorch Tensor
        output
        the nb x nb triangular factor of the block reflector
    """
    # todo: what is k? where is this defined???? which dimension of v is this?
    # V is of size: m, nb
    # T is of size: nb, nb -> k is nb
    # todo: v must be 2D
    m, nb = v.shape
    t = torch.eye(nb)
    if m == 0:
        # what am I returning here???
        return t
    t[0, 0] = tau[0]
    for i in range(1, nb):
        if tau[i] == 0:
            t[[j for j in range(i)], i] = 0
        else:
            # todo: check the setting of t, originally in fortran
            t[0:i, i] = -1 * tau[i] * v[i:m, 0:i].t() @ v[i:m, i]

            t[0:i, i] = t[0:i, 0:i] @ t[0:i, i]
        t[i, i] = tau[i]

    return t


def form_w(v, tau, old_w=None):
    if old_w is None:
        if len(v.shape) == 1:
            w = (-1 * tau[0] * v).unsqueeze_(1)
            return w
        w = -1 * tau[0] * v[:, 0]
        w = torch.unsqueeze(w, 1)

    else:
        w = old_w
    # this is assuming that the w is not split in the 0th dimension
    for i in range(1, v.shape[1]):
        y = v[:, :i]
        # print(w.shape)
        # print(i, y)
        # prev_q = torch.eye(v.shape[0]) + w @ y.t()
        # print(prev_q)
        # print(i, (torch.eye(v.shape[0]) + w @ y.t()).shape, v[:, i].unsqueeze_(1).shape)
        c = -1 * tau[i] * (torch.eye(v.shape[0]) + w @ y.t()) @ v[:, i].unsqueeze_(1)
        if len(c.shape) == 1:
            c.unsqueeze_(1)
        # print(c)
        w = torch.cat((w, c), dim=1)
    return w


def matmul(a, b):
    """
    Matrix multiplication of two DNDarrays

    for comment context -> a @ b = c or A @ B = c

    Parameters
    ----------
    a : ht.DNDarray
        2 dimensional: L x P
    b : ht.DNDarray
        2 dimensional: P x Q

    Returns
    -------
    ht.DNDarray
        returns a tensor with the result of a @ b. The split dimension of the returned array is typically the split dimension of a.
        However, if a.split = None then the the c.split will be set as the split dimension of b. If both are None then c.split is also None.
        ** NOTE ** if a is a split vector then the returned vector will be of shape (1xQ) and will be split in the 1st dimension
        ** NOTE ** if b is a vector and either a or b is split, then the returned vector will be of shape (Lx1) and will be split in the 0th dimension

    References
    ----------
    [1] R. Gu, et al., "Improving Execution Concurrency of Large-scale Matrix Multiplication on Distributed Data-parallel Platforms,"
        IEEE Transactions on Parallel and Distributed Systems, vol 28, no. 9. 2017.
    [2] S. Ryu and D. Kim, "Parallel Huge Matrix Multiplication on a Cluster with GPGPU Accelerators,"
        2018 IEEE International Parallel and Distributed Processing Symposium Workshops (IPDPSW), Vancouver, BC, 2018, pp. 877-882.

    Example
    -------
    >>> a = ht.ones((n, m), split=1)
    >>> a[0] = ht.arange(1, m + 1)
    >>> a[:, -1] = ht.arange(1, n + 1)
    (0/1) tensor([[1., 2.],
                  [1., 1.],
                  [1., 1.],
                  [1., 1.],
                  [1., 1.]])
    (1/1) tensor([[3., 1.],
                  [1., 2.],
                  [1., 3.],
                  [1., 4.],
                  [1., 5.]])
    >>> b = ht.ones((j, k), split=0)
    >>> b[0] = ht.arange(1, k + 1)
    >>> b[:, 0] = ht.arange(1, j + 1)
    (0/1) tensor([[1., 2., 3., 4., 5., 6., 7.],
                  [2., 1., 1., 1., 1., 1., 1.]])
    (1/1) tensor([[3., 1., 1., 1., 1., 1., 1.],
                  [4., 1., 1., 1., 1., 1., 1.]])
    >>> linalg.matmul(a, b)
    (0/1) tensor([[18.,  8.,  9., 10.],
                  [14.,  6.,  7.,  8.],
                  [18.,  7.,  8.,  9.],
                  [22.,  8.,  9., 10.],
                  [26.,  9., 10., 11.]])
    (1/1) tensor([[11., 12., 13.],
                  [ 9., 10., 11.],
                  [10., 11., 12.],
                  [11., 12., 13.],
                  [12., 13., 14.]])
    """
    if a.gshape[-1] != b.gshape[0]:
        raise ValueError("If the last dimension of a ({}) is not the same size as the second-to-last dimension of b. ({})".format(a.gshape[-1], b.gshape[-2]))

    # determine if a larger type is needed for c
    if a.dtype is types.float64 or b.dtype is types.float64:
        c_type = types.float64
    elif (a.dtype is types.int64 and b.dtype is types.int) or (b.dtype is types.int64 and a.dtype is types.int):
        c_type = types.int64
    else:
        c_type = types.float

    if a.split is None and b.split is None:  # matmul from torch
        if len(a.gshape) < 2 or len(b.gshape) < 2:
            # if either of A or B is a vector
            return factories.array(torch.matmul(a._DNDarray__array, b._DNDarray__array))
        else:
            a = a.resplit(0)
            slice_0 = a.comm.chunk(a.shape, a.split)[2][0]
            hold = a._DNDarray__array @ b._DNDarray__array

            c = factories.zeros((a.gshape[-2], b.gshape[1]), dtype=c_type)
            c._DNDarray__array[slice_0.start:slice_0.stop, :] += hold
            c.comm.Allreduce(MPI.IN_PLACE, c, MPI.SUM)
            return c
    else:
        # if they are vectors they need to be expanded to be the proper dimensions
        if len(a.gshape) < 2:
            a = manipulations.expand_dims(a, axis=0)
        if len(b.gshape) < 2:
            b = manipulations.expand_dims(b, axis=1)
        split_0_flag = False
        split_1_flag = False
        split_01_flag = False
        split_10_flag = False

        if (a.split == 0 and b.split is None) or (a.split is None and b.split == 1):
            c = factories.zeros((a.gshape[-2], b.gshape[1]), split=a.split if a.split is not None else b.split, dtype=c_type)
            c._DNDarray__array += a._DNDarray__array @ b._DNDarray__array
            return c

        elif a.split == 1 and b.split is None:
            c = torch.zeros((a.gshape[-2], b.gshape[1]), dtype=c_type.torch_type())
            a_idx = a.comm.chunk(a.shape, a.split)[2]
            c += a._DNDarray__array @ b._DNDarray__array[a_idx[1].start:a_idx[1].start + a.lshape[-1], :]
            a.comm.Allreduce(MPI.IN_PLACE, c, MPI.SUM)
            return factories.array(c, split=a.split if b.gshape[1] > 1 else 0)

        elif a.split is None and b.split == 0:
            c = torch.zeros((a.gshape[-2], b.gshape[1]), dtype=c_type.torch_type())
            b_idx = b.comm.chunk(b.shape, b.split)[2]
            c += a._DNDarray__array[:, b_idx[0].start:b_idx[0].start + b.lshape[0]] @ b._DNDarray__array
            b.comm.Allreduce(MPI.IN_PLACE, c, MPI.SUM)
            return factories.array(c, split=b.split if a.gshape[-2] > 1 else 1)

        elif a.split == 0 and b.split == 0:
            split_0_flag = True
        elif a.split == 1 and b.split == 1:
            split_1_flag = True
        elif a.split == 0 and b.split == 1:
            split_01_flag = True
        elif a.split == 1 and b.split == 0:
            split_10_flag = True
        else:
            raise NotImplementedError('splits > 1 not implemented')

        # block sizes dont need to be the same. thy just need the same inner dimmension (kB)
        kB = 0
        rem_a, rem_b = [0] * 2
        if a.split == len(a.gshape)-1 and b.split == len(a.gshape)-2:  # if the split direction is the last dim in a and the first dim in b
            # the max inner dim (kB) is the min value from the result of the integer division of the last dim of a/world size and the first dim of b/world size
            kB = min([a.gshape[-1] // a.comm.size, b.gshape[0] // b.comm.size])
        elif a.split == len(a.gshape) - 2 and b.split == len(a.gshape) - 1:
            kB = a.gshape[-1]
        elif a.split == len(a.gshape)-1:
            kB = a.gshape[-1] // a.comm.size
        elif b.split == len(a.gshape)-2:
            kB = b.gshape[0] // b.comm.size
            kB = kB if kB < a.gshape[-1] else a.gshape[-1]

        if a.lshape[-1] % kB != 0:
            rem_a = 1
        if b.lshape[0] % kB != 0:
            rem_b = 1

        # get the lshape map to determine what needs to be sent where as well as M and N
        # lshape map dims -> {node, a=0, b=1, lshape}
        lshape_map = factories.zeros((a.comm.size, 2, len(a.gshape)), dtype=int)
        lshape_map[a.comm.rank, 0, :] = torch.Tensor(a.lshape)
        lshape_map[b.comm.rank, 1, :] = torch.Tensor(b.lshape)
        a.comm.Allreduce(MPI.IN_PLACE, lshape_map, MPI.SUM)

        # find mB (first blocking dim for a) and nB (2nd blocking dim for b)
        mB = lshape_map[:, 0, -2].min().item()
        nB = lshape_map[:, 1, -1].min().item()

        # check for remaining dims in the outside dimensions
        rem_a_out, rem_b_out = 0, 0
        if a.lshape[-2] % mB != 0:
            rem_a_out = 1
        if b.lshape[-1] % nB != 0:
            rem_b_out = 1

        # get the flags from all processes
        # rem_map dims guide -> {process number, a/b (0/1), True/False (1/0) if there is a remainder in this dimension
        rem_map = factories.zeros((a.comm.size, 2, 2))
        rem_map[a.comm.rank, 0, :] = (rem_a_out, rem_a)
        rem_map[a.comm.rank, 1, :] = (rem_b, rem_b_out)
        rem_map_comm = a.comm.Iallreduce(MPI.IN_PLACE, rem_map, MPI.SUM)

        # index_map dims guide -> {process number, a=0/b=1, relevent 1st index, 2nd index}
        index_map = factories.zeros((a.comm.size, 2, 2, 2), dtype=int)
        a_idx = a.comm.chunk(a.shape, a.split)[2]
        index_map[a.comm.rank, 0, 0] = (a_idx[0].start, a_idx[0].stop)
        index_map[a.comm.rank, 0, 1] = (a_idx[1].start, a_idx[1].stop)
        b_idx = b.comm.chunk(b.shape, b.split)[2]
        index_map[b.comm.rank, 1, 0] = (b_idx[0].start, b_idx[0].stop)
        index_map[b.comm.rank, 1, 1] = (b_idx[1].start, b_idx[1].stop)
        index_map_comm = a.comm.Iallreduce(MPI.IN_PLACE, index_map, MPI.SUM)

        # for the communication scheme, the output array needs to be created
        c_shape = (a.gshape[-2], b.gshape[1])
        c = factories.zeros(c_shape, split=a.split, dtype=c_type)

        # get the index map for c
        c_index_map = factories.zeros((c.comm.size, 2, 2))
        c_idx = c.comm.chunk(c.shape, c.split)[2]
        c_index_map[c.comm.rank, 0, :] = (c_idx[0].start, c_idx[0].stop)
        c_index_map[c.comm.rank, 1, :] = (c_idx[1].start, c_idx[1].stop)
        c_wait = c.comm.Iallreduce(MPI.IN_PLACE, c_index_map, MPI.SUM)

        if a.split == 0:
            a_block_map = torch.zeros((a.comm.size, a.shape[-2] // mB // a.comm.size, a.shape[-1] // kB, 2))
        elif a.split == 1:
            a_block_map = torch.zeros((a.comm.size, a.shape[-2] // mB, a.shape[-1] // kB // a.comm.size, 2))
        # units-> [process, dim0 block number, dim1 block number, start coord] **indices are local
        index_map_comm.wait()
        for pr in range(a.comm.size):
            start0 = index_map[pr, 0, 0, 0].item()
            stop0 = index_map[pr, 0, 0, 1].item()
            start1 = index_map[pr, 0, 1, 0].item()
            stop1 = index_map[pr, 0, 1, 1].item()

            for dim0 in range((stop0 - start0) // mB):
                # loop over the number of blocks in the 0th dimension
                for dim1 in range((stop1 - start1) // kB):
                    # loop over the number of blocks in the 1st dimension
                    a_block_map[pr, dim0, dim1] = torch.tensor((dim0 * mB, dim1 * kB), dtype=torch.int)
        rem_map_comm.wait()
        if b.split == 0:
            # the blocks are shifted in the 2nd dimension of A for as many remainders there are between the blocks in the first dim of B
            cnt = 0
            for r in rem_map[:, 1, 0]:
                if r.item():
                    cnt += 1
                    a_block_map[:, :, cnt:, 1] += 1

        if b.split == 0:
            b_block_map = torch.zeros((b.comm.size, b.shape[-2] // kB // b.comm.size, b.shape[-1] // nB, 2))
        if b.split == 1:
            b_block_map = torch.zeros((b.comm.size, b.shape[-2] // kB, b.shape[-1] // nB // b.comm.size, 2))
        # units-> [process, dim0 block number, dim1 block number, start coord] **indices are local
        for pr in range(b.comm.size):
            start0 = index_map[pr, 1, 0, 0].item()
            stop0 = index_map[pr, 1, 0, 1].item()
            start1 = index_map[pr, 1, 1, 0].item()
            stop1 = index_map[pr, 1, 1, 1].item()

            for dim0 in range((stop0 - start0) // kB):
                # loop over the number of blocks in the 0th dimension
                for dim1 in range((stop1 - start1) // nB):
                    # loop over the number of blocks in the 1st dimension
                    b_block_map[pr, dim0, dim1] = torch.tensor((dim0 * kB, dim1 * nB), dtype=torch.int)

        if a.split == 1:
            cnt = 0
            # this loop will push the blocks in B to adjust for the remainders in A
            for r in rem_map[:, 0, 1]:
                if r.item():
                    cnt += 1
                    b_block_map[:, cnt:, :, 0] += 1

        def c_block_setter(b_proc, a_proc, a_data, b_data):
            shp_b = list(b_block_map.shape)
            offset_a = b_proc * shp_b[1] if b_proc != 0 else 0
            shp_a = list(a_block_map.shape)
            offset_b = a_proc * shp_a[2] if a_proc != 0 else 0
            # offsets are the number of blocks in the multiplication direction on previous nodes

            for bl_1_a in range(offset_a, offset_a + shp_b[1]) if b.split == 0 else range(a_block_map[a_proc].shape[0]):
                # this offset is the number of blocks on the previous node in the direction of multiplication
                for bl_0_a in range(a_block_map[a_proc].shape[0]):  # dim0
                    for bl_1_b in range(b_block_map[b_proc].shape[1]):
                        for bl_0_b in range(offset_b, offset_b + shp_a[1]) if a.split == 1 else range(b_block_map[b_proc].shape[0]):
                            # this offset is the same as before but for b
                            a_start1 = int(a_block_map[a_proc, bl_0_a, bl_1_a, 1].item())
                            a_start0 = int(a_block_map[a_proc, bl_0_a, bl_1_a, 0].item())
                            a_block = a_data[a_start0:a_start0 + mB, a_start1:a_start1 + kB]

                            b_start0 = int(b_block_map[b_proc, bl_0_b, bl_1_b, 0].item())
                            b_start1 = int(b_block_map[b_proc, bl_0_b, bl_1_b, 1].item())
                            b_block = b_data[b_start0:b_start0 + kB, b_start1:b_start1 + nB]

                            c_start0 = a_start0
                            c_start1 = b_start1
                            c._DNDarray__array[c_start0:c_start0 + mB, c_start1:c_start1 + nB] += a_block @ b_block

        # work loop: loop over all processes (also will incorporate the remainder calcuations)
        rem_map = rem_map._DNDarray__array
        c_wait.wait()

        if split_0_flag:
            # need to send b here and not a
            b_rem_locs0 = (rem_map[:, 1, 0] == 1).nonzero()  # locations of the remainders in b
            a_rem_locs0 = (rem_map[:, 0, 0] == 1).nonzero()
            a_node_rem_s0 = a._DNDarray__array[:mB, kB:(kB + 1) * b_rem_locs0.numel():kB + 1]  # remainders for a in the
            b_rem = torch.empty(b_rem_locs0.numel(), b.lshape[-1], dtype=a.dtype.torch_type())

            # this if/elif/else loop is for the handling of
            if a.comm.rank in a_rem_locs0:
                # if A is split in dim0 and the rank has a remainder in this direction
                r = a._DNDarray__array[-1]
                r_loc = index_map[a.comm.rank, 0, 0, 1] - index_map[a.comm.rank, 0, 0, 0] - 1
            else:
                r = None
                r_loc = None

            req = {}
            b_lp_data = {}
            for pr in range(b.comm.size):
                # ibcast data on node first
                if b.comm.rank == pr:
                    b_lp_data[pr] = b._DNDarray__array
                else:
                    b_lp_data[pr] = torch.zeros((lshape_map[pr, 1, 0].item(), lshape_map[pr, 1, 1].item()), dtype=b.dtype.torch_type())

                # sending a to all nodes for b to operate with
                req[pr] = b.comm.Ibcast(b_lp_data[pr], root=pr)

                # receive the data from the last loop and do the calculation with that
                if pr != 0:
                    req[pr - 1].wait()
                    # after receiving the last loop's bcast
                    c_block_setter(b_proc=pr - 1, a_proc=a.comm.rank, a_data=a._DNDarray__array, b_data=b_lp_data[pr-1])

                    # check if there is a remainder on b in the previous node
                    if pr - 1 in b_rem_locs0:  # this loop is intended to get the remainders of b since it is the one being passed
                        b_rem[pr - 1] = b_lp_data[pr - 1][-1]  # takes care of the remainders in b as well as dim0 of a

                    if a_rem_locs0.nelement() != 0:  # this loop is to take care of the remainders in dim0 of A
                        if r_loc is not None:
                            st = index_map[pr - 1, 1, 0, 0].item()
                            sp = index_map[pr - 1, 1, 0, 1].item()
                            c._DNDarray__array[r_loc.item(), :] += r[st:sp] @ b_lp_data[pr - 1]

                    del b_lp_data[pr - 1]

                # need to wait if its the last loop, also need to collect the remainders
                if pr == b.comm.size - 1:
                    req[pr].wait()
                    c_block_setter(b_proc=pr, a_proc=a.comm.rank, a_data=a._DNDarray__array, b_data=b_lp_data[pr])
                    # check if there is a remainder on b on the last node (there shouldnt be)
                    if pr in b_rem_locs0:
                        b_rem[pr] = b_lp_data[pr][-1]  # this is to save the data from B required by the remainders from dim1 of A

                    if a_rem_locs0.nelement() != 0:  # this loop is to take care of the remainders in the 0th dimension of A
                        if r_loc is not None:
                            st = index_map[pr, 1, 0, 0].item()
                            sp = index_map[pr, 1, 0, 1].item()

                            if split_01_flag:
                                st1 = index_map[pr, 1, 1, 0].item()
                                sp1 = index_map[pr, 1, 1, 1].item()
                                c._DNDarray__array[r_loc.item(), st1:sp1] += r[st:sp] @ b_lp_data[pr]
                            else:
                                c._DNDarray__array[r_loc.item(), :] += r[st:sp] @ b_lp_data[pr]

                    # set the final blocks on the last loop, then adjust for the the remainders which were collected in b_rem
                    if b_rem_locs0.numel():
                        c._DNDarray__array[:a_node_rem_s0.shape[0]] += a_node_rem_s0 @ b_rem

                    del b_lp_data[pr]
            return c

        elif split_1_flag:
            # for this case, a is sent to b
            b_rem_locs1 = (rem_map[:, 1, 1] == 1).nonzero()  # locations of the remainders in b
            a_rem_locs1 = (rem_map[:, 0, 1] == 1).nonzero()
            b_node_rem_s1 = b._DNDarray__array[kB:(kB + 1) * a_rem_locs1.numel():kB + 1, : nB]  # remainders for a in the
            a_rem = torch.empty(a.lshape[-2], a_rem_locs1.numel(), dtype=b.dtype.torch_type())

            # this if/elif/else loop is for the handling of
            if b.comm.rank in b_rem_locs1:
                # if b is split in dim1 and the rank has a remainder in this direction
                r = b._DNDarray__array[:, -1]
                r_loc = index_map[a.comm.rank, 1, 1, 1] - index_map[a.comm.rank, 1, 1, 0] - 1
            else:
                r = None
                r_loc = None

            req = {}
            a_lp_data = {}
            for pr in range(a.comm.size):
                # ibcast data on node first
                if a.comm.rank == pr:
                    a_lp_data[pr] = a._DNDarray__array
                else:
                    a_lp_data[pr] = torch.zeros((lshape_map[pr, 0, 0].item(), lshape_map[pr, 0, 1].item()), dtype=a.dtype.torch_type())

                # sending a to all nodes for b to operate with
                req[pr] = a.comm.Ibcast(a_lp_data[pr], root=pr)

                # receive the data from the last loop and do the calculation with that
                if pr != 0:
                    # after receiving the last loop's bcast
                    req[pr - 1].wait()
                    c_block_setter(a_proc=pr - 1, b_proc=b.comm.rank, a_data=a_lp_data[pr - 1], b_data=b._DNDarray__array)

                    # check if there is a remainder on b in the previous node
                    if pr - 1 in a_rem_locs1:  # this loop is intended to get the remainders of b since it is the one being passed
                        a_rem[:, pr - 1] = a_lp_data[pr - 1][:, -1]  # takes care of the remainders in b as well as dim0 of a

                    if b_rem_locs1.nelement() != 0:  # this loop is to take care of the remainders in dim1 of B
                        if r_loc is not None:
                            st = index_map[pr - 1, 0, 1, 0].item()
                            sp = index_map[pr - 1, 0, 1, 1].item()
                            c._DNDarray__array[:, r_loc.item()] += (a_lp_data[pr - 1] @ r[st:sp, None]).flatten()

                    del a_lp_data[pr - 1]

                # need to wait if its the last loop, also need to collect the remainders
                if pr == b.comm.size - 1:
                    req[pr].wait()
                    c_block_setter(a_proc=pr, b_proc=a.comm.rank, a_data=a_lp_data[pr], b_data=b._DNDarray__array)
                    # check if there is a remainder on b on the last node (there shouldnt be)
                    if pr in a_rem_locs1:
                        a_rem[:, pr] = a_lp_data[pr][:, -1]  # this is to save the data from B required by the remainders from dim1 of A

                    if b_rem_locs1.nelement() != 0:  # this loop is to take care of the remainders in the 0th dimension of A
                        if r_loc is not None:
                            st = index_map[pr, 0, 1, 0].item()
                            sp = index_map[pr, 0, 1, 1].item()
                            c._DNDarray__array[:, r_loc.item()] += (a_lp_data[pr] @ r[st:sp, None]).flatten()

                    # set the final blocks on the last loop, then adjust for the the remainders which were collected in b_rem
                    if a_rem_locs1.numel():
                        c._DNDarray__array[:, :b_node_rem_s1.shape[1]] += a_rem @ b_node_rem_s1

                    del a_lp_data[pr]
            return c

        elif split_01_flag:
            # for this case there are no remainders which need to be taken care of
            req = {}
            b_lp_data = {}
            for pr in range(a.comm.size):
                # ibcast data on node first
                if b.comm.rank == pr:
                    b_lp_data[pr] = b._DNDarray__array
                else:
                    b_lp_data[pr] = torch.empty((lshape_map[pr, 1, 0].item(), lshape_map[pr, 1, 1].item()), dtype=b.dtype.torch_type())

                # sending a to all nodes for b to operate with
                req[pr] = b.comm.Ibcast(b_lp_data[pr], root=pr)

                # receive the data from the last loop and do the calculation with that
                if pr != 0:
                    req[pr - 1].wait()
                    # after receiving the last loop's bcast
                    st0 = index_map[pr - 1, 0, 0, 0].item()
                    sp0 = index_map[pr - 1, 0, 0, 1].item() + 1
                    st1 = index_map[pr - 1, 1, 1, 0].item()
                    sp1 = index_map[pr - 1, 1, 1, 1].item()
                    c._DNDarray__array[:sp0-st0, st1:sp1] += a._DNDarray__array @ b_lp_data[pr - 1]

                    del b_lp_data[pr - 1]

                if pr == b.comm.size - 1:
                    req[pr].wait()
                    st0 = index_map[pr, 0, 0, 0].item()
                    sp0 = index_map[pr, 0, 0, 1].item() + 1
                    st1 = index_map[pr, 1, 1, 0].item()
                    sp1 = index_map[pr, 1, 1, 1].item()
                    c._DNDarray__array[:sp0-st0, st1:sp1] += a._DNDarray__array @ b_lp_data[pr]

                    del b_lp_data[pr]
            return c

        elif split_10_flag:
            # for this case, only a sum is needed at the end
            a_rem_locs1 = (rem_map[:, 0, 1] == 1).nonzero()
            b_rem_locs0 = (rem_map[:, 1, 0] == 1).nonzero()  # locations of the remainders in b
            res = torch.zeros((a.gshape[-2], b.gshape[1]), dtype=c_type.torch_type())
            res += a._DNDarray__array[:mB, :kB] @ b._DNDarray__array[:kB, :nB]
            if a.comm.rank in a_rem_locs1 and b.comm.rank in b_rem_locs0:
                res += a._DNDarray__array[:, -1, None] @ b._DNDarray__array[None, -1, :]  # these Nones are used to change the dims

            a.comm.Allreduce(MPI.IN_PLACE, res, MPI.SUM)

            return factories.array(res, split=a.split if b.gshape[-1] > 1 else 0)


def qr(a, copy=True, output=None):
    """
    Compute the qr factorization of a matrix.
    Factor the matrix a as qr, where q is orthonormal and r is upper-triangular.

    :param a:
    :param output:
    :return:
    NOTE : to get it so that the input is in the proper shape (split=1), take the transpose if q=0
           this means that A.T = QR and A = Q"R" where Q" = Q (orthogonal) and R" = Q.T @ R.T @ Q.T
           ...it is unlikely that this works.... need to verify and change if necessary
    """
    '''
    assuming split=1 for now (for visualization purposes)
    take the QR of the column, then distribute Q to the other processes and do matmul of the rest
    
    1. run geqrf on the first node
    2. calculate v by getting the tril from a and setting the diagonal to 1
    2b. save R to the output matrix
    3. send v to other processes (from rank + 1 to size)
        * need to slice the data in the proper shape for what matters on each process...
    4. (base) calculate w with the form w formula
    SPLIT IN PROCESSES
    5. (base) send w to other processes (from rank + 1 to size)
    6. figure out a way to save and update Q
    
    4. (rest) multiple v.t by the rest of the dataset
    5. receive w and multiply w @ (v.t @ A2) <- done already
    6. cut top n rows (n = the number of vectors passed in v) and save to output_r
    7. update Q???
    8. cut the rows across all processes and repeat with n-1 processes    '''
    if copy:
        a = a.copy()
    if not isinstance(a, dndarray.DNDarray):
        raise TypeError('\'a\' must be a DNDarray')

    lshape_map = factories.zeros((a.comm.size, len(a.gshape)), dtype=int)
    lshape_map[a.comm.rank, :] = torch.Tensor(a.lshape)
    a.comm.Allreduce(MPI.IN_PLACE, lshape_map, MPI.SUM)

    # need to get the shape of the data on other nodes
    rank = a.comm.rank
    r_out = torch.zeros(a.lshape, dtype=a.dtype.torch_type())
    # need to keep track of the previous columns to adjust for the amount to slice off the top
    prev_cols = 0
    for pr in range(a.comm.size):
        if pr == a.comm.size - 1:
            if pr == rank:
                # print('h', a._DNDarray__array[prev_cols:, :].shape)
                a_geqrf, tau = a._DNDarray__array[prev_cols:, :].geqrf()
                # set the r_out from the geqrf function
                r_out[prev_cols:, :] = a_geqrf.triu()
            return factories.array(r_out, is_split=a.split, device=a.device, comm=a.comm, dtype=types.canonical_heat_type(r_out.dtype))

        equal = True if rank == pr else False
        greater = True if rank > pr else False
        less_eq = True if rank <= pr else False

        if equal:
            a_geqrf, tau = a._DNDarray__array[prev_cols:, :].geqrf()
            # set the r_out from the geqrf function
            r_out[prev_cols:, :] = a_geqrf.triu()
            # get v from the lower triangular portion of a,
            v = a_geqrf.tril()
            v_mask = torch.eye(min(v.shape), dtype=a.dtype.torch_type())
            dims_to_pad = [i - j for i, j in zip(v.shape, v_mask.shape)]

            dims_to_pad = [0, dims_to_pad[1], 0, dims_to_pad[0]]
            v_mask = torch.nn.functional.pad(v_mask, dims_to_pad, "constant", 0)
            v.masked_fill_(v_mask.byte(), 1)
        else:
            # todo: if differing chunk size need to change the second item here
            v = torch.empty((lshape_map[pr, 0].item() - prev_cols, lshape_map[pr, 1].item()), dtype=a.dtype.torch_type())
            t = torch.empty((lshape_map[pr, 1].item(), lshape_map[pr, 1].item()), dtype=a.dtype.torch_type())

        req_v = a.comm.Ibcast(v, root=pr)
        if equal:
            t = larft(v, tau)
        # print(pr, v.shape, t.shape)
        # print('t', t)
        req_t = a.comm.Ibcast(t, root=pr)

        if greater:
            req_v.wait()
            # todo: replace the rest of a with v.t @ a
            # print('vshape', v.shape)
            w = v.t() @ a._DNDarray__array[prev_cols:, :]
            req_t.wait()
            # print(a)
            a._DNDarray__array[prev_cols:, :] -= v @ t.t() @ w
            r_out = a._DNDarray__array[:, :lshape_map[pr, 1].item() + 1]
            # print('r', rank, r_out)

        prev_cols += lshape_map[pr, 1].item()  # todo: change if diff block size
        # print(prev_cols)


def transpose(a, axes=None):
    """
    Permute the dimensions of an array.

    Parameters
    ----------
    a : array_like
        Input array.
    axes : None or list of ints, optional
        By default, reverse the dimensions, otherwise permute the axes according to the values given.

    Returns
    -------
    p : ht.DNDarray
        a with its axes permuted.
    """
    # type check the input tensor
    if not isinstance(a, dndarray.DNDarray):
        raise TypeError('a must be of type ht.DNDarray, but was {}'.format(type(a)))

    # set default value for axes permutations
    dimensions = len(a.shape)
    if axes is None:
        axes = tuple(reversed(range(dimensions)))
    # if given, sanitize the input
    else:
        try:
            # convert to a list to allow index access
            axes = list(axes)
        except TypeError:
            raise ValueError('axes must be an iterable containing ints')

        if len(axes) != dimensions:
            raise ValueError('axes do not match tensor shape')
        for index, axis in enumerate(axes):
            if not isinstance(axis, int):
                raise TypeError('axis must be an integer, but was {}'.format(type(axis)))
            elif axis < 0:
                axes[index] = axis + dimensions

    # infer the new split axis, it is the position of the split axis within the new axes permutation
    try:
        transposed_split = axes.index(a.split) if a.split is not None else None
    except ValueError:
        raise ValueError('axes do not match tensor shape')

    # try to rearrange the tensor and return a new transposed variant
    try:
        transposed_data = a._DNDarray__array.permute(*axes)
        transposed_shape = tuple(a.shape[axis] for axis in axes)

        return dndarray.DNDarray(transposed_data, transposed_shape, a.dtype, transposed_split, a.device, a.comm)
    # if not possible re- raise any torch exception as ValueError
    except (RuntimeError, IndexError) as exception: 
        raise ValueError(str(exception))


# statically allocated index slices for non-iterable dimensions in triangular operations
__index_base = (slice(None), slice(None),)


def __tri_op(m, k, op):
    """
    Generic implementation of triangle operations on tensors. It takes care of input sanitation and non-standard
    broadcast behavior of the 2D triangle-operators.

    Parameters
    ----------
    m : ht.DNDarray
        Input tensor for which to compute the triangle operator.
    k : int, optional
        Diagonal above which to apply the triangle operator, k<0 is below and k>0 is above.
    op : callable
        Implementation of the triangle operator.

    Returns
    -------
    triangle_tensor : ht.DNDarray
        DNDarray with the applied triangle operation

    Raises
    ------
    TypeError
        If the input is not a tensor or the diagonal offset cannot be converted to an integral value.
    """
    if not isinstance(m, dndarray.DNDarray):
        raise TypeError('Expected m to be a tensor but was {}'.format(type(m)))

    try:
        k = int(k)
    except ValueError:
        raise TypeError('Expected k to be integral, but was {}'.format(type(k)))

    # chunk the global shape of the tensor to obtain the offset compared to the other ranks
    offset, _, _ = m.comm.chunk(m.shape, m.split)
    dimensions = len(m.shape)

    # manually repeat the input for vectors
    if dimensions == 1:
        triangle = m._DNDarray__array.expand(m.shape[0], -1)
        if torch.numel(triangle > 0):
            triangle = op(triangle, k - offset)

        return dndarray.DNDarray(
            triangle,
            (m.shape[0], m.shape[0],),
            m.dtype,
            None if m.split is None else 1,
            m.device,
            m.comm
        )

    original = m._DNDarray__array
    output = original.clone()

    # modify k to account for tensor splits
    if m.split is not None:
        if m.split + 1 == dimensions - 1:
            k += offset
        elif m.split == dimensions - 1:
            k -= offset

    # in case of two dimensions we can just forward the call to the callable
    if dimensions == 2:
        if torch.numel(original) > 0:
            op(original, k, out=output)
    # more than two dimensions: iterate over all but the last two to realize 2D broadcasting
    else:
        ranges = [range(elements) for elements in m.lshape[:-2]]
        for partial_index in itertools.product(*ranges):
            index = partial_index + __index_base
            op(original[index], k, out=output[index])

    return dndarray.DNDarray(output, m.shape, m.dtype, m.split, m.device, m.comm)


def tril(m, k=0):
    """
    Returns the lower triangular part of the tensor, the other elements of the result tensor are set to 0.

    The lower triangular part of the tensor is defined as the elements on and below the diagonal.

    The argument k controls which diagonal to consider. If k=0, all elements on and below the main diagonal are
    retained. A positive value includes just as many diagonals above the main diagonal, and similarly a negative
    value excludes just as many diagonals below the main diagonal.

    Parameters
    ----------
    m : ht.DNDarray
        Input tensor for which to compute the lower triangle.
    k : int, optional
        Diagonal above which to zero elements. k=0 (default) is the main diagonal, k<0 is below and k>0 is above.

    Returns
    -------
    lower_triangle : ht.DNDarray
        Lower triangle of the input tensor.
    """
    return __tri_op(m, k, torch.tril)


def triu(m, k=0):
    """
    Returns the upper triangular part of the tensor, the other elements of the result tensor are set to 0.

    The upper triangular part of the tensor is defined as the elements on and below the diagonal.

    The argument k controls which diagonal to consider. If k=0, all elements on and below the main diagonal are
    retained. A positive value includes just as many diagonals above the main diagonal, and similarly a negative
    value excludes just as many diagonals below the main diagonal.

    Parameters
    ----------
    m : ht.DNDarray
        Input tensor for which to compute the upper triangle.
    k : int, optional
        Diagonal above which to zero elements. k=0 (default) is the main diagonal, k<0 is below and k>0 is above.

    Returns
    -------
    upper_triangle : ht.DNDarray
        Upper triangle of the input tensor.
    """
    return __tri_op(m, k, torch.triu)
