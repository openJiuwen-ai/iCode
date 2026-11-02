# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AIxCoding customization at the service tier.

Service-layer features whose dependencies pin them above the kernel tier
(``chrys.kernel.aixcoding``) but which touch no upstream file beyond their
registered wiring points. Current occupant: :mod:`chrys.service.aixcoding.catalog`
(server-owned model catalog; see ``aixcoding/docs/batches/catalog-sync/design.md``).
"""
