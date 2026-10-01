"""Mini-défi — moteur de recherche d'offres en ligne de commande.

Exemples :
  python search.py "développeur python"
  python search.py "données spark" --ville Lyon --contrat CDI --salaire-min 45000
  python search.py "kubernetes" --autour 43.6108,3.8767 --rayon 50km --teletravail partiel
"""

from __future__ import annotations

import argparse

from es_client import INDEX, get_client


def construire_requete(args: argparse.Namespace) -> dict:
    must = [{
        "multi_match": {
            "query": args.texte,
            "fields": ["titre^3", "competences.texte^2", "description"],
            "fuzziness": "AUTO",  # tolère les fautes de frappe
        }
    }]
    filtres: list[dict] = []  # contexte filtre : pas de score, mis en cache
    for champ in ("ville", "contrat", "teletravail"):
        valeur = getattr(args, champ)
        if valeur:
            filtres.append({"term": {champ: valeur}})
    if args.salaire_min:
        filtres.append({"range": {"salaire_max": {"gte": args.salaire_min}}})
    if args.autour:
        lat, lon = (float(x) for x in args.autour.split(","))
        filtres.append({"geo_distance": {"distance": args.rayon, "localisation": {"lat": lat, "lon": lon}}})
    return {"bool": {"must": must, "filter": filtres}}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("texte", help="mots-clés recherchés")
    p.add_argument("--ville")
    p.add_argument("--contrat", choices=["CDI", "CDD", "Alternance", "Freelance", "Stage"])
    p.add_argument("--teletravail", choices=["aucun", "partiel", "total"])
    p.add_argument("--salaire-min", type=int)
    p.add_argument("--autour", help="lat,lon")
    p.add_argument("--rayon", default="30km")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--taille", type=int, default=10)
    args = p.parse_args()

    es = get_client()
    resp = es.search(
        index=INDEX,
        query=construire_requete(args),
        from_=(args.page - 1) * args.taille,
        size=args.taille,
        source=["titre", "entreprise", "ville", "contrat", "salaire_min", "salaire_max"],
        highlight={"fields": {"description": {"fragment_size": 120, "number_of_fragments": 1}},
                   "pre_tags": ["\033[1m"], "post_tags": ["\033[0m"]},
        aggs={
            "par_ville": {"terms": {"field": "ville", "size": 5}},
            "par_contrat": {"terms": {"field": "contrat"}},
            "top_competences": {"terms": {"field": "competences", "size": 8}},
        },
        track_total_hits=True,
    )

    total = resp["hits"]["total"]["value"]
    print(f"{total} offre(s) — page {args.page}\n")
    for hit in resp["hits"]["hits"]:
        s = hit["_source"]
        salaire = f"{s['salaire_min']}–{s['salaire_max']} €" if "salaire_min" in s else "salaire n.c."
        print(f"[{hit['_score']:.2f}] {s['titre']} — {s['entreprise']} ({s['ville']}, {s['contrat']}, {salaire})")
        for frag in hit.get("highlight", {}).get("description", []):
            print(f"        …{frag}…")
    print("\nFacettes :")
    for nom, agg in resp["aggregations"].items():
        print(f"  {nom} : " + ", ".join(f"{b['key']} ({b['doc_count']})" for b in agg["buckets"]))


if __name__ == "__main__":
    main()
