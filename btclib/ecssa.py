#!/usr/bin/env python3

# Copyright (C) 2017-2019 The btclib developers
#
# This file is part of btclib. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution.
#
# No part of btclib including this file, may be copied, modified, propagated,
# or distributed except according to the terms contained in the LICENSE file.

""" Elliptic Curve Schnorr Signature Algorithm

https://github.com/sipa/bips/blob/bip-schnorr/bip-schnorr.mediawiki
"""

import heapq
from hashlib import sha256
from typing import List, Optional

from btclib.numbertheory import mod_inv, legendre_symbol
from btclib.ec import Union, Tuple, Scalar, Point, GenericPoint, to_Point, \
    EllipticCurve, secp256k1, _jac_from_aff, _pointMultJacobian, pointMult, \
    DblScalarMult, int_from_Scalar, bytes_from_Point
from btclib.rfc6979 import rfc6979
from btclib.ecsigutils import HashLengthBytes, bytes_from_hlenbytes, \
    int_from_hlenbytes

ECSS = Tuple[int, Scalar]  # Tuple[Coordinate, Scalar]


def ecssa_sign(m: bytes,
               d: Scalar,
               k: Optional[Scalar] = None,
               ec: EllipticCurve = secp256k1,
               hf = sha256) -> Tuple[int, int]:
    """ECSSA signing operation according to bip-schnorr"""

    # the bitcoin proposed standard is only valid for curves
    # whose prime p = 3 % 4
    if not ec.pIsThreeModFour:
        errmsg = 'curve prime p must be equal to 3 (mod 4)'
        raise ValueError(errmsg)

    # This signature scheme supports 32-byte messages.
    # Differently from ECDSA, the 32-byte message can be
    # a digest of other messages, but it does not need to.

    # The message m: a 32-byte array
    m = bytes_from_hlenbytes(m, hf)

    # The secret key d: an integer in the range 1..n-1.
    d = int_from_Scalar(ec, d)
    if d == 0:
        raise ValueError("invalid (zero) private key")
    Q = pointMult(ec, d, ec.G)

    if k is None:
        k = rfc6979(d, m, ec, hf)
    else:
        k = int_from_Scalar(ec, k)

    # Fail if k' = 0.
    if k == 0:
        raise ValueError("ephemeral key k=0 in ecssa sign operation")
    # Let R = k'G.
    R = pointMult(ec, k, ec.G)

    # Let k = k' if jacobi(y(R)) = 1, otherwise let k = n - k' .
    # break the simmetry: any criteria might have been used,
    # jacobi is the proposed bitcoin standard
    if legendre_symbol(R[1], ec._p) != 1:
        # no need to actually change R[1], as it is not used anymore
        # let just fix k instead, as it is used later
        k = ec.n - k

    # Let e = int(hf(bytes(x(R)) || bytes(dG) || m)) mod n.
    ebytes = R[0].to_bytes(ec.bytesize, byteorder="big")
    ebytes += bytes_from_Point(ec, Q, True)
    ebytes += m
    ebytes = hf(ebytes).digest()
    e = int_from_hlenbytes(ebytes, ec, hf)

    s = (k + e*d) % ec.n  # s=0 is ok: in verification there is no inverse of s
    # The signature is bytes(x(R)) || bytes(k + ed mod n).
    return R[0], s


def ecssa_verify(ssasig: ECSS,
                 m: bytes,
                 Q: GenericPoint,
                 ec: EllipticCurve = secp256k1,
                 hf = sha256) -> bool:
    """ECSSA veryfying operation according to bip-schnorr"""

    # this is just a try/except wrapper
    # _ecssa_verify raises Errors
    try:
        return _ecssa_verify(ssasig, m, Q, ec, hf)
    except Exception:
        return False

# Private function provided for testing purposes only.
# It raises Errors, while verify should always return True or False


def _ecssa_verify(ssasig: ECSS,
                  m: HashLengthBytes,
                  P: GenericPoint,
                  ec: EllipticCurve = secp256k1,
                  hf = sha256) -> bool:
    # ECSSA veryfying operation according to bip-schnorr

    # the bitcoin proposed standard is only valid for curves
    # whose prime p = 3 % 4
    if not ec.pIsThreeModFour:
        errmsg = 'curve prime p must be equal to 3 (mod 4)'
        raise ValueError(errmsg)

    # Let r = int(sig[0:32]); fail if r ≥ p.
    # Let s = int(sig[32:64]); fail if s ≥ n.
    r, s = to_ssasig(ssasig, ec)

    # The message m: a 32-byte array
    m = bytes_from_hlenbytes(m, hf)

    # Let P = point(pk); fail if point(pk) fails.
    P = to_Point(ec, P)

    # Let e = int(hf(bytes(r) || bytes(P) || m)) mod n.
    ebytes = r.to_bytes(ec.bytesize, byteorder="big")
    ebytes += bytes_from_Point(ec, P, True)
    ebytes += m
    ebytes = hf(ebytes).digest()
    e = int_from_hlenbytes(ebytes, ec, hf)

    # Let R = sG - eP.
    R = DblScalarMult(ec, s, ec.G, -e, P)

    # Fail if infinite(R).
    if R[1] == 0:
        raise ValueError("sG - eP is infinite")
    # Fail if jacobi(y(R)) ≠ 1.
    if legendre_symbol(R[1], ec._p) != 1:
        raise ValueError("y(sG - eP) is not a quadratic residue")
    # Fail if x(R) ≠ r.
    return R[0] == r


def _ecssa_pubkey_recovery(ssasig: ECSS,
                           ebytes: bytes,
                           ec: EllipticCurve = secp256k1,
                           hf = sha256) -> Point:

    if len(ebytes) != hf().digest_size:
        raise ValueError("wrong size for e")

    r, s = to_ssasig(ssasig, ec)

    K = (r, ec.yQuadraticResidue(r, True))
    e = int_from_hlenbytes(ebytes, ec, hf)
    if e == 0:
        raise ValueError("invalid (zero) challenge e")
    e1 = mod_inv(e, ec.n)
    Q = DblScalarMult(ec, e1*s, ec.G, -e1, K)
    if Q[1] == 0:
        raise ValueError("failed")
    return Q


def to_ssasig(ssasig: ECSS,
              ec: EllipticCurve = secp256k1) -> Tuple[int, int]:
    """check SSA signature format is correct and return the signature itself"""

    # A signature sig: a 64-byte array.
    if len(ssasig) != 2:
        m = "invalid length %s for ECSSA signature" % len(ssasig)
        raise TypeError(m)

    # Let r = int(sig[0:32]); fail if r ≥ p.
    r = int(ssasig[0])
    # r is in [0, p-1]
    # ec.checkCoordinate(r)
    # it might be too much, but R.x is valid iif R.y does exist
    ec.y(r)

    # Let s = int(sig[32:64]); fail if s ≥ n.
    s = int(ssasig[1])
    if not (0 <= s < ec.n):
        raise ValueError("s not in [0, n-1]")

    return r, s


def ecssa_batch_validation(sig: List[ECSS],
                           ms: List[bytes],
                           Q: List[Point],
                           a: List[int],
                           ec: EllipticCurve = secp256k1,
                           hf = sha256) -> bool:
    # initialization
    mult = 0
    points = list()
    factors = list()

    u = len(Q)
    for i in range(u):
        r, s = to_ssasig(sig[i], ec)
        ebytes = r.to_bytes(32, byteorder="big")
        ebytes += bytes_from_Point(ec, Q[i], True)
        ebytes += ms[i]
        ebytes = hf(ebytes).digest()
        e = int_from_hlenbytes(ebytes, ec, hf)

        y = ec.y(r)  # raises an error if y does not exist

        mult += a[i] * s % ec.n
        points.append(_jac_from_aff((r, y)))
        factors.append(a[i])
        points.append(_jac_from_aff(Q[i]))
        factors.append(a[i] * e % ec.n)

    # Bos-coster's algorithm, source:
    # https://cr.yp.to/badbatch/boscoster2.py
    boscoster = list(zip([-n for n in factors], points))
    heapq.heapify(boscoster)
    while len(boscoster) > 1:
        aK1 = heapq.heappop(boscoster)
        aK2 = heapq.heappop(boscoster)
        a1, K1 = -aK1[0], aK1[1]
        a2, K2 = -aK2[0], aK2[1]
        K2 = ec._addJacobian(K1, K2)
        a1 -= a2
        if a1 > 0:
            heapq.heappush(boscoster, (-a1, K1))
        heapq.heappush(boscoster, (-a2, K2))
    aK = heapq.heappop(boscoster)

    RHSJ = _pointMultJacobian(ec, -aK[0], aK[1])
    TJ = _pointMultJacobian(ec, mult, _jac_from_aff(ec.G))
    RHS = ec._affine_from_jac(RHSJ)
    T = ec._affine_from_jac(TJ)

    return  T == RHS
