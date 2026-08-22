# ============================================================
# BnesBrowser 完整建構流程腳本
# 執行：pwsh -NoProfile -ExecutionPolicy Bypass -File E:\BnesBrowser-build\run_remaining.ps1
# 產物：E:\BnesBrowser-build\BnesBrowser_setup.exe
#
# 可選環境變數：
#   SKIP_SYNC=1     略過 bnes-brave-core -> src\brave 映射同步
#   SKIP_HOOKS=1     略過 gclient runhooks
#   SKIP_GN=1        略過 gn gen
#   SKIP_BRANDING=1  略過 branding 複製（不建議）
#   NINJA_JOBS=12    平行編譯數
#
# > Chromium：純編譯基底，不修改上游邏輯。
# > Brave overlay：brave/bnes/ 維持插入式分叉，不修改 Chromium 上游核心。
# > 映射：在地合併複製（merge/overwrite，不刪既有檔案），避免對
#   node_modules、node-win-x64、wintun 等 hook 產物造成破壞。
# ============================================================

param(
    [switch]$MapOnly   # 只執行 bnes-brave-core -> src\brave 映射，然後結束（不建構）。
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$BuildRoot = 'E:\BnesBrowser-build'
$SrcDir    = Join-Path $BuildRoot 'src'
$BraveDir  = Join-Path $SrcDir 'brave'
$OutDir    = Join-Path $SrcDir "out\Release_GN"
$OutName   = 'Release_GN'
$SetupName = 'BnesBrowser_setup.exe'
$SetupPath = Join-Path $BuildRoot $SetupName

# ── 環境變數 ────────────────────────────────────────────────
$depotTools = @(
    (Join-Path $BraveDir 'vendor\depot_tools')
    (Join-Path $SrcDir 'third_party\depot_tools')
) | Where-Object { Test-Path $_ }
$env:PATH = ($depotTools + $env:PATH) -join ';'
$env:DEPOT_TOOLS_WIN_TOOLCHAIN = '0'
$env:GYP_MSVS_VERSION = '2022'
$env:PYTHONPATH = @(
    (Join-Path $BraveDir 'script')
    (Join-Path $SrcDir 'tools\grit\grit\extern')
    (Join-Path $BraveDir 'vendor\requests')
    (Join-Path $BraveDir 'third_party\cryptography')
    (Join-Path $BraveDir 'third_party\macholib')
    (Join-Path $SrcDir 'build')
    (Join-Path $SrcDir 'third_party\depot_tools')
    $env:PYTHONPATH
) -join ';'
$env:PYTHONUNBUFFERED        = '1'
$env:PYTHONUTF8              = '1'
$env:GSUTIL_ENABLE_LUCI_AUTH = '0'
$env:RUSTUP_HOME             = Join-Path $SrcDir 'third_party\rust-toolchain'

Set-Location $BuildRoot
Write-Host "=== cwd: $((Get-Location).Path) ===" -ForegroundColor Cyan

function Get-EnvFlag([string]$Name) {
    $v = [Environment]::GetEnvironmentVariable($Name)
    return ($v -eq '1' -or $v -eq 'true')
}

# ── 同步(映射) BNES 自訂源碼 ────────────────────────────────
# 將 bnes-brave-core 全程「在地合併複製」到 src\brave。與舊版差異：
#   * 以遞迴函式 Copy-BnesTree 逐檔合併，能在「來源是資料夾、目標同名
#     已是檔案」時先移除目標 leaf，解決 Copy-Item 的
#     "Container cannot be copied onto existing leaf item"。
#   * 只做 merge/overwrite，絕不刪除目標中**來源沒有**的檔案/資料夾，
#     因此 node_modules、node-win-x64、wintun 等 hook 產物不會被誤刪。
#   * 根層檔案（BUILD.gn / DEPS / package.json / pnpm-* / tsconfig* 等）
#     一併映射，確保 build tree 與 bnes-brave-core 一致。
$BnesCore = 'S:\Ai_Agent\BNES\bnes-brave-core'
$syncIgnore = @('.git', '.claude', '.github', '.agents', '.gemini', 'node_modules', 'win_build_output')

# 遞迴合併：以來源為主，覆寫目標；遇型別衝突先清目標同名 leaf。
function Copy-BnesTree {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Dest,
        [string[]]$IgnoreNames = @()
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Container)) { return }

    # 確保目的根是「資料夾」：若 $Dest 已存在但其型別是檔案（leaf），
    # 代表目的端有同名檔案/資料夾衝突，需先移除再重建為資料夾。
    if (Test-Path -LiteralPath $Dest) {
        if (-not (Test-Path -LiteralPath $Dest -PathType Container)) {
            Remove-Item -LiteralPath $Dest -Force
        }
    }
    if (-not (Test-Path -LiteralPath $Dest -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    }

    $top = Get-ChildItem -LiteralPath $Source -Force -ErrorAction SilentlyContinue
    foreach ($item in $top) {
        if ($IgnoreNames -contains $item.Name) { continue }

        $target = Join-Path $Dest $item.Name
        $parentDir = Split-Path -Parent $target

        if ($item.PSIsContainer) {
            # 來源是資料夾：若目標為檔案 -> 清除該 leaf 衝突，再建立資料夾。
            if (Test-Path -LiteralPath $target -PathType Leaf) {
                Remove-Item -LiteralPath $target -Force
            }
            if (-not (Test-Path -LiteralPath $target -PathType Container)) {
                # 先確保父目錄存在，否則 New-Item 無法建立中間路徑。
                if (-not (Test-Path -LiteralPath $parentDir -PathType Container)) {
                    New-Item -ItemType Directory -Force -Path $parentDir | Out-Null
                }
                New-Item -ItemType Directory -Force -Path $target | Out-Null
            }
            Copy-BnesTree -Source $item.FullName -Dest $target -IgnoreNames $IgnoreNames
        }
        else {
            # 來源是檔案：若目標為資料夾，則移除整棵目標容器後改放檔案。
            if (Test-Path -LiteralPath $target -PathType Container) {
                Remove-Item -LiteralPath $target -Recurse -Force
            }
            if (-not (Test-Path -LiteralPath $parentDir -PathType Container)) {
                New-Item -ItemType Directory -Force -Path $parentDir | Out-Null
            }
            Copy-Item -LiteralPath $item.FullName -Destination $target -Force
        }
    }
}

function Sync-BnesTree {
    Write-Host "`n[SYNC] 從 bnes-brave-core 映射所有源碼到 src\brave\ ..." -ForegroundColor Yellow

    if (-not (Test-Path -LiteralPath $BraveDir -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $BraveDir | Out-Null
        Write-Host "[SYNC] src\brave 不存在，已建立。" -ForegroundColor Green
    }

    foreach ($d in (Get-ChildItem -LiteralPath $BnesCore -Force | Where-Object { $_.PSIsContainer -and $syncIgnore -notcontains $_.Name })) {
        Copy-BnesTree -Source $d.FullName -Dest (Join-Path $BraveDir $d.Name) -IgnoreNames $syncIgnore
        Write-Host "[SYNC] $($d.Name)\ -> src\brave\$($d.Name)\ 完成。" -ForegroundColor Green
    }

    # 根層檔案（檔案型）一併映射，確保 BUILD.gn / DEPS / package.json 等與核心一致。
    foreach ($f in (Get-ChildItem -LiteralPath $BnesCore -Force | Where-Object { -not $_.PSIsContainer -and $syncIgnore -notcontains $_.Name })) {
        $target = Join-Path $BraveDir $f.Name
        if (Test-Path -LiteralPath $target -PathType Container) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
        Copy-Item -LiteralPath $f.FullName -Destination $target -Force
    }
    Write-Host "[SYNC] 映射完成。" -ForegroundColor Green
}

$needSync = -not (Get-EnvFlag 'SKIP_SYNC')
# 若關鍵腳本檔 build.ts 缺失，務必重新映射，避免後續 branding 失敗。
if ($needSync -and -not (Test-Path (Join-Path $BraveDir 'build\commands\scripts\build.ts'))) {
    Write-Host "[SYNC] src\brave\build 缺 build.ts，強制重新映射。" -ForegroundColor Yellow
    $needSync = $true
}
if (-not $needSync) {
    Write-Host "`n[SYNC] 已略過映射（SKIP_SYNC）。" -ForegroundColor DarkYellow
}
else {
    Sync-BnesTree
}

if ($MapOnly) {
    Write-Host "[MAP] 映射完成。因指定 -MapOnly，腳本到此結束（不繼續建構）。" -ForegroundColor Cyan
    exit 0
}

# ── 步驟 1：gclient runhooks ──────────────────────────────
$hooksLog = Join-Path $BuildRoot 'hooks3.log'
$skipHooks = (Get-EnvFlag 'SKIP_HOOKS') -or (
    (Test-Path $hooksLog) -and
    (Select-String -Path $hooksLog -Pattern '====GCLIENT_EXIT=0====' -Quiet -ErrorAction SilentlyContinue)
)
if ($skipHooks) {
    Write-Host "`n[STEP 1] gclient runhooks 已完成，略過。" -ForegroundColor DarkYellow
    $hooksExit = 0
} else {
    Write-Host "`n[STEP 1] gclient runhooks ..." -ForegroundColor Yellow
    Set-Location $BuildRoot
    gclient runhooks 2>&1 | Tee-Object -FilePath $hooksLog
    $hooksExit = $LASTEXITCODE
    Add-Content $hooksLog "====GCLIENT_EXIT=$hooksExit===="
    Write-Host "[STEP 1] EXIT=$hooksExit" -ForegroundColor $(if ($hooksExit -eq 0) { 'Green' } else { 'Red' })
}

# ── 步驟 2：確認關鍵 hook 產物 ───────────────────────────
Write-Host "`n[STEP 2] 確認關鍵 hook 產物 ..." -ForegroundColor Yellow
$checks = @(
    @{Name='brave/build/version.gni';                     Path=(Join-Path $BraveDir 'build\version.gni')},
    @{Name='build/util/LASTCHANGE';                       Path=(Join-Path $SrcDir 'build\util\LASTCHANGE')},
    @{Name='brave/third_party/wintun/bin/x64/wintun.dll'; Path=(Join-Path $BraveDir 'third_party\wintun\bin\x64\wintun.dll')},
    @{Name='wireguard-nt';                                Path=(Join-Path $BraveDir 'third_party\brave-vpn-wireguard-nt-dlls')},
    @{Name='wireguard-tunnel';                            Path=(Join-Path $BraveDir 'third_party\brave-vpn-wireguard-tunnel-dlls')},
    @{Name='node-win-x64';                                Path=(Join-Path $BraveDir 'third_party\node\node-win-x64')}
)
$missingCritical = $false
foreach ($c in $checks) {
    $ok = Test-Path $c.Path
    $color = if ($ok) { 'Green' } else { 'Red' }
    Write-Host "  [$( if ($ok) {'OK'} else {'MISSING'} )] $($c.Name)" -ForegroundColor $color
    if (-not $ok -and $c.Name -match 'node|midl') { $missingCritical = $true }
}
if ($missingCritical) {
    Write-Host "`n[WARN] 有關鍵 hook 產物缺失，建議先排查 hooks3.log 再繼續建構。" -ForegroundColor Red
}

# ── 步驟 2.5：編譯 Brave redirect_cc（chromium_src 覆寫必要）──
$redirectDir = Join-Path $SrcDir 'out\redirect_cc'
$redirectExe = Join-Path $redirectDir 'redirect_cc.exe'
$redirectLog = Join-Path $BuildRoot 'redirect_cc.log'

$mainArgsGn = Join-Path $OutDir 'args.gn'
$useSiso = $false
if (Test-Path $mainArgsGn) {
    $useSiso = (Select-String -Path $mainArgsGn -Pattern '^\s*use_siso\s*=\s*true' -Quiet)
}

if ($useSiso) {
    Write-Host "`n[STEP 2.5] use_siso=true，Siso 自動重映射 brave/chromium_src/*.cc，略過 redirect_cc。" -ForegroundColor DarkYellow
    $redirectExit = 0
} elseif (Test-Path $redirectExe) {
    Write-Host "`n[STEP 2.5] redirect_cc 已存在，略過編譯。" -ForegroundColor DarkYellow
    $redirectExit = 0
} else {
    Write-Host "`n[STEP 2.5] 編譯 Brave redirect_cc ..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Force -Path $redirectDir | Out-Null
    @'
import("//brave/tools/redirect_cc/args.gni")
use_remoteexec = false
use_siso = false
real_rewrapper = "E:/BnesBrowser-build/src/buildtools/reclient/rewrapper"
# Redirect_cc build must not use Chromium's default translate_genders=true;
# on non-ios it defaults true and triggers "Input to target not generated by
# a dependency" for the FEMININE/MASCULINE/NEUTER gender paks that a redirect
# build does not generate. Brave defaults.gni already disables this for the
# same reason. Keep parity with the main Release_GN args.gn.
translate_genders = false
enable_pseudolocales = false
'@ | Set-Content -Path (Join-Path $redirectDir 'args.gn') -Encoding UTF8
    Set-Location $SrcDir
    gn gen out/redirect_cc 2>&1 | Tee-Object -FilePath $redirectLog
    ninja -C out/redirect_cc brave/tools/redirect_cc -j12 2>&1 | Tee-Object -FilePath $redirectLog -Append
    $redirectExit = $LASTEXITCODE
    Add-Content $redirectLog "====REDIRECT_CC_EXIT=$redirectExit===="
    if (-not (Test-Path $redirectExe)) {
        Write-Host "[STEP 2.5] redirect_cc.exe 未產出，見 $redirectLog" -ForegroundColor Red
        exit $(if ($redirectExit) { $redirectExit } else { 1 })
    }
    Write-Host "[STEP 2.5] redirect_cc 就緒：$redirectExe" -ForegroundColor Green
}

# ── 步驟 3：gn gen ────────────────────────────────────────
Write-Host "`n[STEP 3] 建立 gn 建構目錄 ..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$argsGn = Join-Path $OutDir 'args.gn'
if (-not (Test-Path $argsGn)) {
    Write-Host "  建立 args.gn ..." -ForegroundColor Cyan
    @'
# BnesBrowser Release build args
import("//brave/build/args/brave_defaults.gni")
import("//brave/build/args/branding_defaults.gni")

is_official_build = false
is_debug = false
is_component_build = false
target_cpu = "x64"

# 品牌設定 - BnesBrowser
brave_product_name = "BnesBrowser"

# 停用不需要的功能以加速建構
enable_widevine = false
treat_warnings_as_errors = false
enable_pseudolocales = false

# Windows 安裝檔設定
build_omaha = false
skip_signing = true

# 本機 Siso（Brave 預設）。Siso 會重映射 brave/chromium_src/*.cc。
use_remoteexec = false
use_siso = true

# 符號（安裝檔建構不需要 full symbols）
symbol_level = 0
blink_symbol_level = 0

# 本機無 PGO profiles；關閉 official PGO
chrome_pgo_phase = 0

# BNES 不使用 Brave 遠端服務；非空字串僅為通過官方 assert
brave_services_key = "BNES_PLACEHOLDER"
service_key_stt = "BNES_PLACEHOLDER"
service_key_search = "BNES_PLACEHOLDER"
service_key_aichat = "BNES_PLACEHOLDER"
uphold_production_api_url = "https://placeholder"
zebpay_production_api_url = "https://placeholder"
translate_genders = false
'@ | Set-Content -Path $argsGn -Encoding UTF8
    Write-Host "  args.gn 已建立：$argsGn" -ForegroundColor Green
} else {
    Write-Host "  args.gn 已存在，略過建立。" -ForegroundColor Cyan
    Get-Content $argsGn | Write-Host
}

$gnLog = Join-Path $BuildRoot 'gn_gen.log'
$skipGn = (Get-EnvFlag 'SKIP_GN') -or (
    (Test-Path (Join-Path $OutDir 'build.ninja')) -and
    (Test-Path $gnLog) -and
    (Select-String -Path $gnLog -Pattern '====GN_GEN_EXIT=0====' -Quiet -ErrorAction SilentlyContinue)
)
if ($skipGn) {
    Write-Host "  gn gen 已完成，略過。" -ForegroundColor DarkYellow
    $gnExit = 0
} else {
    Write-Host "`n  執行 gn gen ..." -ForegroundColor Cyan
    Set-Location $SrcDir
    gn gen "out/$OutName" 2>&1 | Tee-Object -FilePath $gnLog
    $gnExit = $LASTEXITCODE
    Add-Content $gnLog "====GN_GEN_EXIT=$gnExit===="
    Write-Host "[STEP 3] gn gen EXIT=$gnExit" -ForegroundColor $(if ($gnExit -eq 0) { 'Green' } else { 'Red' })
}

if ($gnExit -ne 0) {
    Write-Host "`n[ERROR] gn gen 失敗，請查看 $gnLog" -ForegroundColor Red
    Write-Host "腳本中止，等待排查。" -ForegroundColor Red
    exit $gnExit
}

# ── 步驟 3.5：Brave branding 複製（安裝檔必要）────────────
# ninja 需要 chrome/app/brave_strings.grd 等 overlay 檔；
# 這些由 brave/build/commands/lib/branding.js 從 src/brave 複製到 Chromium 樹。
$brandLog = Join-Path $BuildRoot 'branding.log'
$brandingOk = Test-Path (Join-Path $SrcDir 'chrome\app\brave_strings.grd')
if ((Get-EnvFlag 'SKIP_BRANDING') -and $brandingOk) {
    Write-Host "`n[STEP 3.5] branding 已存在，略過。" -ForegroundColor DarkYellow
    $brandExit = 0
} else {
    Write-Host "`n[STEP 3.5] 更新 Brave branding（複製 brave_strings.grd 等）..." -ForegroundColor Yellow
    Set-Location $BraveDir
    node ./build/commands/scripts/build.ts --prepare_only -C $OutName --skip_signing 2>&1 |
        Tee-Object -FilePath $brandLog
    $brandExit = $LASTEXITCODE
    Add-Content $brandLog "====BRANDING_EXIT=$brandExit===="
    $brandingOk = Test-Path (Join-Path $SrcDir 'chrome\app\brave_strings.grd')
    if ($brandingOk) {
        Write-Host "[STEP 3.5] chrome/app/brave_strings.grd 已就緒" -ForegroundColor Green
    } else {
        Write-Host "[STEP 3.5] EXIT=$brandExit，且 brave_strings.grd 仍缺失。見 $brandLog" -ForegroundColor Red
        Write-Host "腳本中止：沒有 branding 檔無法封裝安裝檔。" -ForegroundColor Red
        exit $(if ($brandExit) { $brandExit } else { 1 })
    }
}

# ── 步驟 4：ninja 建構安裝檔 ──────────────────────────────
Write-Host "`n[STEP 4] ninja 建構 BnesBrowser 安裝檔 ..." -ForegroundColor Yellow
$ninjaLog = Join-Path $BuildRoot 'ninja.log'
$ninjaTarget = 'create_dist'
$jobs = if ($env:NINJA_JOBS) { $env:NINJA_JOBS } else { '12' }

# wasm-opt-sys (binaryen) needs clang-cl + /EHsc; MSVC 14.42 fails on wasm::Type::isBasic
# and clang-cl without exceptions fails on throw. Do not set env var CL (MSVC flag bag).
$clangCl = Join-Path $SrcDir 'third_party\llvm-build\Release+Asserts\bin\clang-cl.exe'
if (Test-Path $clangCl) {
    $env:CC = $clangCl
    $env:CXX = $clangCl
    $env:CFLAGS = '/EHsc'
    $env:CXXFLAGS = '/EHsc'
}
Remove-Item Env:CL -ErrorAction SilentlyContinue
Remove-Item Env:_CL_ -ErrorAction SilentlyContinue

Set-Location $SrcDir

# 解析 ninja / autoninja：autoninja 不一定在 PATH 上，優先使用
# src\third_party\ninja\ninja.exe 直接呼叫，最可靠。
$ninjaCmd = $null
$ninjaCandidates = @(
    (Join-Path $SrcDir 'third_party\ninja\ninja.exe'),
    (Join-Path $SrcDir 'third_party\depot_tools\ninja.exe'),
    (Join-Path $SrcDir 'third_party\depot_tools\autoninja.bat'),
    (Join-Path $BraveDir 'vendor\depot_tools\ninja.exe'),
    (Join-Path $BraveDir 'vendor\depot_tools\autoninja.bat')
)
foreach ($cand in $ninjaCandidates) {
    if (Test-Path -LiteralPath $cand) { $ninjaCmd = $cand; break }
}
if (-not $ninjaCmd) {
    $cmd = Get-Command ninja -ErrorAction SilentlyContinue
    if ($cmd) { $ninjaCmd = $cmd.Source }
}
if (-not $ninjaCmd) {
    Write-Host "[STEP 4] 找不到 ninja.exe / autoninja！請確認建構工具鏈。見 ninja.log" -ForegroundColor Red
    $ninjaExit = 1
    Add-Content $ninjaLog "====NINJA_EXIT=$($ninjaExit): ninja not found====`n"
}
if ($ninjaCmd) {
    Write-Host "  $ninjaCmd -C out/$OutName $ninjaTarget -j$jobs ..." -ForegroundColor Cyan
    Push-Location $SrcDir
    try {
        & $ninjaCmd -C "out/$OutName" $ninjaTarget "-j$jobs" 2>&1 | Tee-Object -FilePath $ninjaLog
    }
    finally {
        Pop-Location
    }
    $ninjaExit = $LASTEXITCODE
    Add-Content $ninjaLog "====NINJA_EXIT=$ninjaExit===="
    Write-Host "[STEP 4] ninja EXIT=$ninjaExit" -ForegroundColor $(if ($ninjaExit -eq 0) { 'Green' } else { 'Red' })
}

# ── 步驟 5：複製為 BnesBrowser_setup.exe ─────────────────
Write-Host "`n[STEP 5] 封裝 $SetupName ..." -ForegroundColor Yellow
$candidates = @(
    (Join-Path $OutDir 'brave_installer.exe')
    (Join-Path $OutDir 'mini_installer.exe')
    (Join-Path $OutDir 'chrome_installer.exe')
)
$distDir = Join-Path $OutDir 'dist'
if (Test-Path $distDir) {
    $candidates += @(Get-ChildItem $distDir -Filter '*Setup*.exe' -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    $candidates += @(Get-ChildItem $distDir -Filter '*installer*.exe' -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
}

$sourceInstaller = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($ninjaExit -eq 0 -and $sourceInstaller) {
    New-Item -ItemType Directory -Force -Path (Join-Path $OutDir 'dist') | Out-Null
    Copy-Item -Force $sourceInstaller $SetupPath
    Copy-Item -Force $sourceInstaller (Join-Path $OutDir "dist\$SetupName")
    $info = Get-Item $SetupPath
    Write-Host ("  來源 : {0}" -f $sourceInstaller) -ForegroundColor Cyan
    Write-Host ("  輸出 : {0}  ({1:N1} MB)" -f $SetupPath, ($info.Length / 1MB)) -ForegroundColor Green
} elseif ($ninjaExit -eq 0) {
    Write-Host "  ninja 成功但找不到 installer exe。請檢查 $OutDir 與 $distDir" -ForegroundColor Red
    Get-ChildItem $OutDir -Filter '*.exe' -ErrorAction SilentlyContinue |
        Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize | Out-String | Write-Host
    $ninjaExit = 2
}

# ── 完成摘要 ──────────────────────────────────────────────
Write-Host "`n============================================" -ForegroundColor Cyan
Write-Host "  建構完成摘要" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  hooks    : EXIT=$hooksExit  => $hooksLog"
Write-Host "  gn gen   : EXIT=$gnExit     => $gnLog"
Write-Host "  branding : EXIT=$brandExit  => $brandLog"
Write-Host "  ninja    : EXIT=$ninjaExit  => $ninjaLog"
if (Test-Path $SetupPath) {
    Write-Host "  setup    : $SetupPath"
}

if ($ninjaExit -eq 0 -and (Test-Path $SetupPath)) {
    Write-Host "`n[SUCCESS] 安裝檔：$SetupPath" -ForegroundColor Green
    Get-Item $SetupPath |
        Select-Object FullName, LastWriteTime, @{N='Size(MB)';E={[Math]::Round($_.Length/1MB,1)}} |
        Format-Table -AutoSize
} else {
    Write-Host "`n[FAILED] 請查看 $ninjaLog / $brandLog 排查錯誤。" -ForegroundColor Red
    if (Test-Path $ninjaLog) {
        Get-Content $ninjaLog -Tail 30 | Write-Host
    }
    exit $(if ($ninjaExit) { $ninjaExit } else { 1 })
}
