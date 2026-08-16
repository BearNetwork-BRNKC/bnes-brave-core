/* Copyright (c) 2026 The BNES Authors. All rights reserved.
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this file,
 * You can obtain one at http://mozilla.org/MPL/2.0/. */

#ifndef BRAVE_BROWSER_EXTENSIONS_BNES_EXTENSION_CONSTANTS_H_
#define BRAVE_BROWSER_EXTENSIONS_BNES_EXTENSION_CONSTANTS_H_

// BNES PQC 錢包的 Extension ID。
// 此 ID 由 bnes-metamask.pem 私鑰決定，唯一且固定。
// 此常數作為整個 BNES Brave Overlay 中唯一的 ID 真相來源 (single source of truth)。
// 若未來 PEM 金鑰更換，僅需修改此一處。
inline constexpr char kBnesWalletExtensionId[] =
    "ogmlljngfdccnfmieajogmeomikpepmi";

#endif  // BRAVE_BROWSER_EXTENSIONS_BNES_EXTENSION_CONSTANTS_H_
