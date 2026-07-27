# scripts/seed_supply_chains.py
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.sentiment.supply_chain_mapper import load_curated_seed_chains, mine_cooccurrence_chains

def main():
    print("=== MIMIR Project Odin Supply Chain Seeder ===")
    print("Seeding curated supply chain maps into PostgreSQL...")
    count = load_curated_seed_chains()
    print(f"Curated supply chain seeding completed: {count} relationship records active.")
    
    print("\nRunning initial co-occurrence mining...")
    mined = mine_cooccurrence_chains(min_occurrences=3)
    print(f"Co-occurrence mining completed: {mined} candidate relationships added.")
    print("Supply chain setup complete!")

if __name__ == "__main__":
    main()
