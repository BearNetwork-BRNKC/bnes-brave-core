/* Copyright (c) 2026 The Brave Authors. All rights reserved.
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this file,
 * You can obtain one at https://mozilla.org/MPL/2.0/. */

#include "brave/browser/importer/brave_password_importer.h"

#include <utility>

#include "base/check.h"
#include "base/files/file_util.h"
#include "base/functional/bind.h"
#include "base/logging.h"
#include "base/task/thread_pool.h"
#include "brave/common/importer/scoped_copy_file.h"
#include "chrome/browser/browser_process.h"
#include "chrome/browser/password_manager/factories/profile_password_store_factory.h"
#include "components/keyed_service/core/service_access_type.h"
#include "components/os_crypt/async/browser/os_crypt_async.h"
#include "components/password_manager/core/browser/password_store/login_database.h"
#include "components/password_manager/core/browser/password_store/password_store_interface.h"
#include "components/password_manager/core/browser/password_store/stored_credential.h"

namespace {

// Runs on a background thread. Copies the source Login Data file to a
// temporary location (to avoid conflicting with a possibly-running source
// browser), opens it with the destination's Encryptor, and returns the
// decrypted credentials.
std::vector<password_manager::StoredCredential> ReadCredentialsFromLoginData(
    const base::FilePath& login_data_path,
    scoped_refptr<os_crypt_async::Encryptor> encryptor) {
  std::vector<password_manager::StoredCredential> credentials;
  if (!base::PathExists(login_data_path)) {
    return credentials;
  }

  ScopedCopyFile copy_login_data(login_data_path);
  if (!copy_login_data.copy_success()) {
    LOG(ERROR) << "Failed to copy Login Data for password import";
    return credentials;
  }

  password_manager::LoginDatabase database(
      copy_login_data.copied_file_path(),
      password_manager::IsAccountStore(false));
  if (!database.Init(
          /*on_undecryptable_passwords_removed=*/base::NullCallback(),
          std::move(encryptor))) {
    LOG(ERROR) << "LoginDatabase Init() failed for password import";
    return credentials;
  }

  if (!database.GetAutofillableLogins(&credentials)) {
    LOG(ERROR) << "Failed to read autofillable logins for password import";
    credentials.clear();
  }
  std::vector<password_manager::StoredCredential> blocklisted;
  if (database.GetBlocklistLogins(&blocklisted)) {
    credentials.insert(credentials.end(),
                       std::make_move_iterator(blocklisted.begin()),
                       std::make_move_iterator(blocklisted.end()));
  }
  return credentials;
}

}  // namespace

BravePasswordImporter::BravePasswordImporter() = default;
BravePasswordImporter::~BravePasswordImporter() = default;

void BravePasswordImporter::Start(const base::FilePath& source_path,
                                  Profile* destination_profile,
                                  CompletionCallback callback) {
  CHECK(destination_profile);
  CHECK(callback);
  source_path_ = source_path;
  callback_ = std::move(callback);
  password_store_ = ProfilePasswordStoreFactory::GetForProfile(
      destination_profile, ServiceAccessType::EXPLICIT_ACCESS);
  if (!password_store_) {
    std::move(callback_).Run(0);
    return;
  }

  auto* os_crypt = g_browser_process->os_crypt_async();
  if (!os_crypt) {
    std::move(callback_).Run(0);
    return;
  }
  os_crypt->GetInstance(base::BindOnce(&BravePasswordImporter::OnEncryptorReady,
                                       weak_factory_.GetWeakPtr()));
}

void BravePasswordImporter::OnEncryptorReady(
    scoped_refptr<os_crypt_async::Encryptor> encryptor) {
  base::FilePath login_data_path = source_path_.Append(
      base::FilePath::StringType(FILE_PATH_LITERAL("Login Data")));
  base::ThreadPool::PostTaskAndReplyWithResult(
      FROM_HERE, {base::MayBlock(), base::TaskPriority::USER_VISIBLE},
      base::BindOnce(&ReadCredentialsFromLoginData, login_data_path,
                     std::move(encryptor)),
      base::BindOnce(&BravePasswordImporter::OnCredentialsRead,
                     weak_factory_.GetWeakPtr()));
}

void BravePasswordImporter::OnCredentialsRead(
    std::vector<password_manager::StoredCredential> credentials) {
  if (credentials.empty() || !password_store_) {
    std::move(callback_).Run(0);
    return;
  }
  const size_t count = credentials.size();
  password_store_->AddLogins(std::move(credentials),
                             base::BindOnce(std::move(callback_), count));
}
