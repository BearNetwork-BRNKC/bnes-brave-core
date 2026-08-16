/* Copyright (c) 2026 The BNES Authors. All rights reserved.
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this file,
 * You can obtain one at http://mozilla.org/MPL/2.0/. */

// BNES Brave Overlay for install_verifier.cc
//
// 此 overlay 依循 Brave chromium_src 模式：
// 1. 在匿名命名空間中定義我們的 Hook 函數
// 2. 透過 #include 載入原始 Chromium 實作
// 3. 搭配 patches/ 中的 Patch，讓原始實作在 NeedsVerification 中調用我們的 Hook
//
// 上游同步策略：
// - Chromium 上游更新時，本檔案不受影響（屬於 BNES overlay）
// - 若上游修改 NeedsVerification 函數簽名，只需更新對應的 .patch 即可
// - kBnesWalletExtensionId 定義於 bnes_extension_constants.h，為唯一真相來源

#include <string>

#include "brave/browser/extensions/bnes_extension_constants.h"

namespace extensions {

namespace {

// BNES Hook：判斷指定的 extension_id 是否為 BNES PQC 錢包
// 若是，則指示 NeedsVerification 回傳 false，跳過 Web Store 來源驗證
bool IsBnesExtensionBypassedBraveImpl(const std::string& extension_id) {
  return extension_id == kBnesWalletExtensionId;
}

}  // namespace

}  // namespace extensions

// 載入原始 Chromium 的 install_verifier.cc。
// 搭配 patches/chrome-browser-extensions-install_verifier.cc.patch，
// 原始程式碼中的 NeedsVerification 會在進入主體邏輯前先呼叫
// IsBnesExtensionBypassedBraveImpl，若回傳 true 則直接 return false（不需驗證）。
#include <chrome/browser/extensions/install_verifier.cc>
