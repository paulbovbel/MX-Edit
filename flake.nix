{
  description = "MX-Edit desktop editor for HeadRush MX5 and Pedalboard";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      eachSystem = nixpkgs.lib.genAttrs systems;
      packageFor = system: nixpkgs.legacyPackages.${system}.callPackage ./nix/package.nix { };
    in
    {
      packages = eachSystem (
        system:
        let
          package = packageFor system;
        in
        {
          default = package;
          mx-edit = package;
        }
      );

      apps = eachSystem (
        system:
        let
          app = {
            type = "app";
            program = "${self.packages.${system}.mx-edit}/bin/mx-edit";
            meta.description = "Launch the MX-Edit desktop editor";
          };
        in
        {
          default = app;
          mx-edit = app;
        }
      );

      checks = eachSystem (system: {
        mx-edit = self.packages.${system}.mx-edit;
      });
    };
}
