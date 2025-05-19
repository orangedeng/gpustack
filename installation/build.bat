heat.exe dir "dist\main" -o installation\_internal_files.wxs -gg -sfrag -srd -sreg -ke -cg PyinstallerBuiltFiles -dr INSTALLFOLDER -var var.DistDir
candle.exe -dDistDir="dist\main" -dProductVersion="0.6.0.0" -dInstallationDir="installation" .\installation\GPUStack.wxs .\installation\_internal_files.wxs -ext WixUtilExtension -ext WixUIExtension
light.exe -out dist\GPUStackInstaller.msi .\GPUStack.wixobj .\_internal_files.wixobj -ext WixUIExtension -ext WixUtilExtension
