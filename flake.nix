{
  description = "pitkind — LLM committee deliberation CLI";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in {
        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.python311
            pkgs.uv
          ];
          shellHook = ''
            uv sync --all-extras
            source .venv/bin/activate
          '';
        };
      });
}
