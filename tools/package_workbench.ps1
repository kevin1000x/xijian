param(
    [string]$Python = '',
    [string]$Output = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $projectRoot '.tmp\desktop-build-venv\Scripts\python.exe' }
$releaseVersion = (& $Python -B -c "import sys; sys.path.insert(0, sys.argv[1]); from tools.workbench_desktop import VERSION; print(VERSION)" $projectRoot).Trim()
if ($LASTEXITCODE -ne 0 -or $releaseVersion -notmatch '^\d+\.\d+\.\d+-alpha\.\d+$') { throw 'Invalid application version.' }
if (-not $Output) { $Output = Join-Path $projectRoot ('.tmp\desktop-release\' + $releaseVersion) }
$Python = [IO.Path]::GetFullPath($Python)
$Output = [IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath (Join-Path $Output 'Xijian')) {
    throw 'Output already contains a build. Choose a fresh output directory; previous builds are preserved.'
}
if (-not (Test-Path -LiteralPath $Python)) { throw 'Dedicated build environment is missing. See docs/desktop-workbench.md.' }
& $Python -m PyInstaller --distpath $Output --workpath (Join-Path $Output 'build') (Join-Path $projectRoot 'desktop\workbench.spec')
if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed; evidence and intermediate files preserved.' }
$bundle = Join-Path $Output 'Xijian'
& (Join-Path $bundle 'XijianHost.exe') --self-check
if ($LASTEXITCODE -ne 0) { throw 'Bundled runtime self-check failed.' }
$manifest = Get-ChildItem -LiteralPath $bundle -File -Recurse | ForEach-Object {
    [PSCustomObject]@{ path = $_.FullName.Substring($bundle.Length + 1).Replace('\','/'); bytes = $_.Length; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
}
$record = [ordered]@{ version=$releaseVersion; platform='windows-x64'; files=$manifest }
[IO.File]::WriteAllText((Join-Path $Output 'manifest.json'), ($record | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
Write-Output $bundle
Copy-Item -LiteralPath (Join-Path $projectRoot 'desktop\README.txt') -Destination (Join-Path $Output 'README.txt')
$zipCode = @"
import pathlib, zipfile
root=pathlib.Path(__import__('sys').argv[1])
with zipfile.ZipFile(root/'Xijian-$releaseVersion-windows-x64.zip','x',zipfile.ZIP_DEFLATED,compresslevel=1) as archive:
    for path in sorted((root/'Xijian').rglob('*')):
        if path.is_file(): archive.write(path,path.relative_to(root).as_posix())
    for name in ('README.txt','manifest.json'): archive.write(root/name,name)
"@
& $Python -c $zipCode $Output
if ($LASTEXITCODE -ne 0) { throw 'Archive creation failed; bundle preserved.' }
Get-FileHash -LiteralPath (Join-Path $Output "Xijian-$releaseVersion-windows-x64.zip") -Algorithm SHA256