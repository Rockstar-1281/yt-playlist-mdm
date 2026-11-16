param(
    [string]$MdmSource = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$EngineDir = $PSScriptRoot
$AppDir = Split-Path $EngineDir -Parent
if ([string]::IsNullOrWhiteSpace($MdmSource)) {
    $MdmSource = Join-Path $AppDir "..\misterdownloadmanager"
}
$MdmSource = (Resolve-Path -LiteralPath $MdmSource).Path
$SourceFile = Join-Path $EngineDir "src\mdm-video-engine.rs"
$BinDir = Join-Path $MdmSource "crates\cli\src\bin"
$BinSource = Join-Path $BinDir "mdm-video-engine.rs"
$OutputExe = Join-Path $MdmSource "target\release\mdm-video-engine.exe"

foreach ($path in @($SourceFile, (Join-Path $MdmSource "Cargo.toml"), (Join-Path $MdmSource "crates\cli\Cargo.toml"))) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required path not found: $path"
    }
}

$Cargo = Get-Command cargo -ErrorAction SilentlyContinue
if (-not $Cargo) {
    $CargoPath = Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"
    if (Test-Path -LiteralPath $CargoPath) {
        $env:Path = "$(Split-Path $CargoPath -Parent);$env:Path"
        $Cargo = Get-Command cargo -ErrorAction SilentlyContinue
    }
}
if (-not $Cargo) {
    throw "cargo was not found. Install Rust from https://rustup.rs and reopen the terminal."
}

$NasmDir = Join-Path $env:USERPROFILE ".local\nasm"
if (Test-Path -LiteralPath (Join-Path $NasmDir "nasm.exe")) {
    $env:Path = "$NasmDir;$env:Path"
}

$VsCmakeDir = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin"
if (Test-Path -LiteralPath (Join-Path $VsCmakeDir "cmake.exe")) {
    $env:Path = "$VsCmakeDir;$env:Path"
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
Copy-Item -LiteralPath $SourceFile -Destination $BinSource -Force

Push-Location $MdmSource
try {
    & cargo build --release -p mdm-cli --bin mdm-video-engine
    if ($LASTEXITCODE -ne 0) {
        throw "cargo build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Copy-Item -LiteralPath $OutputExe -Destination (Join-Path $EngineDir "mdm-video-engine.exe") -Force
$LicenseSource = Join-Path $MdmSource "LICENSE"
if (Test-Path -LiteralPath $LicenseSource) {
    Copy-Item -LiteralPath $LicenseSource -Destination (Join-Path $EngineDir "LICENSE") -Force
}

$RustcVersion = (& rustc --version).Trim()
$CargoVersion = (& cargo --version).Trim()
$NasmVersion = "not found"
$NasmCommand = Get-Command nasm -ErrorAction SilentlyContinue
if ($NasmCommand) {
    $NasmVersion = (& nasm -v).Trim()
}
$CmakeVersion = "not found"
$CmakeCommand = Get-Command cmake -ErrorAction SilentlyContinue
if ($CmakeCommand) {
    $CmakeVersion = ((& cmake --version) | Select-Object -First 1).Trim()
}
$Commit = (& git -C $MdmSource rev-parse HEAD).Trim()
$CommitDate = (& git -C $MdmSource log -1 --format=%ad --date=short).Trim()
$CommitSubject = (& git -C $MdmSource log -1 --format=%s).Trim()
$Binary = Get-Item -LiteralPath (Join-Path $EngineDir "mdm-video-engine.exe")
$Hash = (Get-FileHash -LiteralPath $Binary.FullName -Algorithm SHA256).Hash
$BuiltAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss 'UTC'")

$BuildInfo = @(
    "mdm-video-engine build metadata",
    "Built: $BuiltAt",
    "MDM source: $MdmSource",
    "MDM commit: $Commit",
    "MDM commit date: $CommitDate",
    "MDM commit subject: $CommitSubject",
    "Rust: $RustcVersion",
    "Cargo: $CargoVersion",
    "CMake: $CmakeVersion",
    "NASM: $NasmVersion",
    "Binary: mdm-video-engine.exe",
    "Size: $($Binary.Length) bytes",
    "SHA256: $Hash",
    "License: GPL-3.0-or-later (see LICENSE)"
)
$BuildInfo | Set-Content -LiteralPath (Join-Path $EngineDir "BUILD.txt") -Encoding UTF8

Write-Output "Built $OutputExe"
Write-Output "Installed $($Binary.FullName) ($($Binary.Length) bytes)"
