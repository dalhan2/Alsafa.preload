param(
    [string]$RepoRoot = $PSScriptRoot,
    [string]$SkillDir = (Join-Path $env:USERPROFILE '.config\opencode\skills\preprocess'),
    [switch]$SetEnv
)

$ErrorActionPreference = 'Stop'

$source = Join-Path $RepoRoot 'SKILL.md'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "SKILL.md not found in $RepoRoot"
}

New-Item -ItemType Directory -Force -Path $SkillDir | Out-Null
$dest = Join-Path $SkillDir 'SKILL.md'
Copy-Item -LiteralPath $source -Destination $dest -Force

if ($SetEnv) {
    $repo = (Resolve-Path -LiteralPath $RepoRoot).Path
    $workRoot = Split-Path -Parent $repo
    [Environment]::SetEnvironmentVariable('PREPROCESS_REPO', $repo, 'User')
    [Environment]::SetEnvironmentVariable('PREPROCESS_ROOT', $workRoot, 'User')
    $env:PREPROCESS_REPO = $repo
    $env:PREPROCESS_ROOT = $workRoot
    Write-Host "Set PREPROCESS_REPO=$repo and PREPROCESS_ROOT=$workRoot for the user."
}

Write-Host "Installed skill: $dest"
