"""
Classe de base pour tous les scripts CGF BRVM30 ETF.
Fournit les chemins standardisés et les helpers JSON.
"""
import json
import os


class BaseScript:
    def __init__(self):
        self.scripts_dir = os.path.dirname(os.path.abspath(__file__))
        self.root_dir    = os.path.normpath(os.path.join(self.scripts_dir, ".."))
        self.data_dir    = os.path.join(self.root_dir, "data")
        self.prix_file   = os.path.join(self.root_dir, "excel", "BRVM_Consolidated_Kendall_updated.xlsx")

    # ------------------------------------------------------------------ #
    # JSON helpers                                                         #
    # ------------------------------------------------------------------ #

    def load_json(self, filename =None):
        """Charge un JSON depuis data_dir. Retourne `default` si absent."""
        path = os.path.join(self.data_dir, filename)
        if not os.path.exists(path):
            return default
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def save_json(self, filename, indent = 2):
        """Écrit un JSON dans data_dir."""
        path = os.path.join(self.data_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)

    def load_json_path(self, path =None):
        """Charge un JSON depuis un chemin absolu."""
        if not os.path.exists(path):
            return default
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def save_json_path(self, path, indent = 2):
        """Écrit un JSON à un chemin absolu."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)

    # ------------------------------------------------------------------ #
    # Point d'entrée à surcharger                                          #
    # ------------------------------------------------------------------ #

    def run(self):
        raise NotImplementedError("Implémenter run() dans la sous-classe.")
