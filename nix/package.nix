{
  lib,
  stdenvNoCC,
  python3,
  makeWrapper,
  makeFontsConf,
  dejavu_fonts,
  xvfb-run,
}:
let
  python = python3.withPackages (ps: [
    ps.tkinter
    ps.pillow
    ps.mido
    ps.python-rtmidi
  ]);
  fonts = makeFontsConf { fontDirectories = [ dejavu_fonts ]; };
in
stdenvNoCC.mkDerivation {
  pname = "mx-edit";
  version = "0.9";
  src = lib.cleanSource ../editor;
  nativeBuildInputs = [ makeWrapper ];
  nativeInstallCheckInputs = [ xvfb-run ];
  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/share/mx-edit" "$out/bin"
    cp -r . "$out/share/mx-edit/"
    makeWrapper ${python}/bin/python "$out/bin/mx-edit" \
      --add-flags "$out/share/mx-edit/mx5editor.py" \
      --set PYTHONNOUSERSITE 1 \
      --set PYTHONDONTWRITEBYTECODE 1 \
      --set-default FONTCONFIG_FILE ${fonts}
    runHook postInstall
  '';

  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
    export FONTCONFIG_FILE=${fonts}
    timeout 30 xvfb-run -a -s "-screen 0 1600x1000x24" \
      ${python}/bin/python ${../tests/nix_smoke.py} "$out/share/mx-edit"
    runHook postInstallCheck
  '';

  meta = {
    description = "Desktop editor for HeadRush MX5 and Pedalboard";
    homepage = "https://github.com/TicT4x/MX-Edit";
    license = lib.licenses.mit;
    mainProgram = "mx-edit";
    platforms = lib.platforms.linux;
  };
}
