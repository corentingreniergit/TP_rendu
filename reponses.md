# Réponses - TP Logstash (EISI)

## Mise en place

**Pourquoi ne pas utiliser le compte `elastic` pour Logstash ?**
`elastic` est un super-utilisateur qui a accès à tous les paramètres du cluster. Logstash n'a besoin que d'écrire dans `offres` et `logs-web-*` et de lire l'état du cluster. Appliquer le principe du moindre privilège limite les dégâts si le mot de passe fuite ou si un pipeline est mal écrit. Un compte dédié se révoque ou se change sans toucher aux comptes d'administration, et il rend les journaux d'audit lisibles.

**Que se passerait-il si le pipeline `web` écrivait dans `logs-generic-default` ?**
Le rôle `logstash_writer` ne couvre que `offres` et `logs-web-*`. Elasticsearch refuserait l'écriture avec une erreur. Le data stream ne serait pas créé non plus. Aucun événement n'arriverait dans l'index, et l'erreur apparaîtrait dans les journaux de Logstash.

**Pourquoi le mot de passe passe-t-il par une variable d'environnement et pas par les `.conf` ?**
Les `.conf` sont versionnés dans Git : un secret écrit dedans serait publié avec le dépôt et resterait dans l'historique même après suppression.

---

## Exercice 0 - Premier pipeline

**Quels champs Logstash a-t-il ajoutés à la phrase ?**
`@timestamp`, `@version`, et, en mode ECS, des métadonnées sur la source : `host` et `event.original`. La phrase elle-même est dans `message`.

**Que contient `@timestamp` ?**
L'heure de création de l'événement par Logstash.

**À quoi sert `--path.data /tmp/essai` ?**
Chaque instance de Logstash a besoin d'un dossier de données exclusif. Deux instances ne peuvent pas partager le même dossier. Avec un dossier temporaire, l'instance éphémère du TP ne gêne pas le Logstash principal et ne touche pas à ses données.

---

## Partie 1 - Recharger les offres

### Exercice 1.2 - Premier lancement

**Les documents sont-ils indexés ?**
Non. Elasticsearch les refuse tous.

**Quelle erreur, quel code, quelle exception ?**
" Could not index event to Elasticsearch ", avec un statut **HTTP 400** et une exception de type `strict_dynamic_mapping_exception`. Le message : *mapping set to strict, dynamic introduction of [xxx] within [_doc] is not allowed*.

**Quels noms de champs sont cités ?**
Ceux que Logstash ajoute : `@version`, `event` (`event.original`), `log` (`log.file.path`), `host` (`host.name`), et `@timestamp`.

**Lien avec `"dynamic": "strict"` :**
Avec `strict`, tout champ absent du mapping est rejeté au lieu d'être ajouté automatiquement. Les champs techniques ajoutés par Logstash n'existent pas dans le mapping de `offres`, donc le document entier est refusé. Aucune offre n'est indexée.

### Exercice 1.3 - Corriger

**Le nombre de documents a-t-il changé ?**
Non : 5 000 avant et après. Le pipeline utilise `document_id => "%{id}"` : chaque offre est écrite sous son `_id` métier (`OFF-xxxxx`), donc elle remplace le document existant au lieu d'en créer un nouveau.

**Et le `_version` de `OFF-00002` ?**
Il augmente de 1.

**Pourquoi supprimer les champs plutôt qu'assouplir le mapping ?**
Pour éviter que des champs inutiles ou mal écrits se retrouvent dans le mapping, ce qui créerait beaucoup de problèmes futurs.

**Pourquoi l'index doit-il exister avant le premier démarrage ?**
Avec `manage_template => false`, Logstash n'installe aucun modèle. Si l'index n'existe pas, Elasticsearch le crée à la première écriture avec un mapping dynamique. Ces types ne se modifient pas après coup, il faudrait supprimer l'index et le recréer. On aurait de plus un index sans le `strict`.

### Exercice 1.4 - Relancer

**Combien de fois le fichier a-t-il été lu ?**
À chaque démarrage, puisque `sincedb_path => "/dev/null"` supprime la mémoire de lecture.

**Avec la sincedb par défaut ?**
Logstash noterait que le fichier a été entièrement lu (en mode `read`, il retient les fichiers terminés, identifiés par leur inode) et ne le relirait pas au redémarrage : `_version` ne bougerait pas. Et si le conteneur est recréé sans volume, la sincedb disparaît.

**Sans `document_id` ?**
Elasticsearch générerait un `_id` aléatoire pour chaque événement. Chaque lecture ajouterait 5 000 nouveaux documents : 10 000 après deux lectures réussies, 15 000 après trois, avec des offres en doublon.

---

## Partie 2 - Superviser et fiabiliser

### Exercice 2.1 - Superviser

**Combien de pipelines, avec combien de workers ?**
Deux : `offres` et `web`. Le nombre de workers vaut par défaut le nombre de cœurs CPU visibles par le conteneur, identique pour les deux sauf réglage contraire.

**Que valent `in`, `filtered`, `out` pour `offres` ?**
5 000 chacun : 5 000 événements lus, 5 000 passés dans les filtres, 5 000 envoyés à Elasticsearch.

### Exercice 2.2 - DLQ

**`OFF-99999` est-il dans l'index ?**
Non. `GET offres/_doc/OFF-99999` renvoie `found: false`, et `_count` reste à 5 000.

**Où se trouve-t-il ?**
Dans la dead letter queue, sur disque, dans `/usr/share/logstash/data/dead_letter_queue/offres/`.

**Quelle raison de refus ?**
Le champ `prime` n'existe pas dans le mapping.

**Qu'apporte la DLQ par rapport à `raise_on_error=False` dans `ingest.py` ?**
Avec `raise_on_error=False`, le script continue mais les erreurs sont seulement renvoyées au code appelant. Si on ne les récupère pas et ne les enregistre pas, elles sont perdues, et il n'y a rien à rejouer. La DLQ conserve le document d'origine complet, avec la raison du refus, la date et le plugin concerné. Elle se relit avec l'entrée `dead_letter_queue`, ce qui permet de corriger puis de réinjecter sans repartir de la source. Sans DLQ, Logstash se contente de journaliser l'erreur et abandonne le document.

**Corriger et réinjecter en trois étapes :**
1. Lire la DLQ avec un pipeline temporaire (`input { dead_letter_queue { path => … pipeline_id => "offres" commit_offsets => true } }`).
2. Corriger l'événement : supprimer `prime` avec `mutate { remove_field => ["prime"] }` ou, si le champ est voulu, l'ajouter d'abord au mapping, et nettoyer les métadonnées de la DLQ.
3. Le réécrire dans `offres` avec la sortie `elasticsearch` (`document_id => "%{id}"`). Le `commit_offsets => true` marque les entrées comme traitées, pour ne pas les rejouer.

### Exercice 2.3 - Pourquoi deux pipelines ?

**Combien de pipelines sans `pipelines.yml` ?**
Un seul, `main`.

**Où va une offre lue dans `offres.ndjson` ?**
Dans tous les filtres et dans toutes les sorties.

**Et une ligne de log ?**
Elle irait aussi dans les deux sorties, donc dans `offres` avec `document_id => "%{id}"` : le champ `id` n'existant pas, tous les logs auraient pour `_id` la chaîne littérale `%{id}` et s'écraseraient les uns les autres, en plus d'être refusés par le mapping strict (`message`, `url`…).

**Deux autres avantages à isoler :**
- **Isolation des pannes et de la charge** : une sortie bloquée ou lente sur un flux n'arrête pas l'autre.
- **Réglages indépendants** : On peut recharger ou corriger un pipeline sans toucher à l'autre.

### Exercice 2.4 - Ne rien perdre

**`docker kill` pendant la lecture, avec la file en mémoire : que deviennent les événements lus mais pas envoyés ?**
Ils sont perdus. La file est en mémoire, et le processus s'arrête sans pouvoir la vider.

**Quel réglage change ce comportement ?**
`queue.type: persisted` dans `pipelines.yml` ou `logstash.yml` : les événements sont écrits sur disque avant d'être acquittés à l'entrée, et rejoués au redémarrage.

**Pourquoi le `document_id` devient-il indispensable ?**
Parce que la garantie « au moins une fois » implique des doublons possibles. Avec un `_id` métier, un événement rejoué écrase le document déjà écrit au lieu d'en créer un second.

---

## Partie 3 - Transformer les logs d'accès

### Exercice 3.2 - Le motif

**Quels champs sont extraits ?**
  "request",
  "agent",
  "auth",
  "ident",
  "verb",
  "referrer",
  "response",
  "bytes",
  "clientip",
  "httpversion",
  "timestamp":

**Quel type pour `http.response.status_code` ?**
Un entier.

**Pourquoi `timestamp` doit-il encore être traité ?**
C'est une simple string. Si on n'y touche pas, `@timestamp` reste l'heure de lecture par Logstash et toute la semaine de logs apparaîtrait comme un seul instant. Le filtre `date` doit l'analyser, la convertir en UTC et l'écrire dans `@timestamp`.

**Motif pour extraire `OFF-01468` de `/offres/OFF-01468/postuler` :**
```text
^/offres/%{OFFRE_ID:offre_id}[0-9]{5}
```

### Exercice 3.4 - Vérifier le data stream

**Combien de documents et d'échecs `grok` ?**
20 700 documents et 0 échec.

**Nom de l'index caché et signification :**
De la forme `.ds-logs-web-default-AAAA.MM.JJ-000001` :
- `.ds-` : préfixe des index de support (*backing indices*) d'un data stream, masqués ;
- `logs-web-default` : nom du data stream (type `logs`, dataset `web`, namespace `default`) ;
- `AAAA.MM.JJ` : date de création de l'index (jour du premier envoi, pas la date des logs) ;
- `000001` : numéro de génération, incrémenté à chaque *rollover*.

**Le premier événement est-il daté du 23/09/2026 à 00:00:39 (+02:00) ?**
Oui.

**Quel type pour `http.response.status_code`, et pourquoi est-ce important ?**
Un type `long`. C'est important pour les comparaisons et les calculs. Avec un champ `keyword`, une comparaison serait lexicographique, et il serait impossible de sommer ou de faire des tranches numériques.

**Quel `index.mode` ?**
`logsdb`.

### Exercice 3.5 - Rejouer sans doublon ?

**Que constatez-vous, et pourquoi pas pour `offres` ?**
Après redémarrage, `_count` passe à 41 400. La sincedb est vide (`/dev/null`), le fichier est relu en entier et chaque ligne devient un nouveau document. Pour `offres`, le `document_id` métier fait que chaque relecture remplace le document existant. Aucun `_id` n'est fixé, donc Elasticsearch en génère un nouveau à chaque fois.

**Peut-on mettre à jour ou remplacer un document dans un data stream ?**
Pas directement : un data stream est en ajout seul. Les écritures passent par l'action `create`. On ne peut pas faire un `index` ni un `update` ciblés sur le nom du data stream. On peut seulement modifier ou supprimer par requête ou en visant directement l'index de support.

**Deux solutions pour rejouer sans doublon :**
1. **Sincedb persistante** : remplacer `/dev/null` par un chemin sur un volume. Le fichier n'est lu qu'une fois, même après redémarrage.

2. **`_id` calculé avec `fingerprint`** : calculer un hachage  du `message` dans `[@metadata][fingerprint]`, puis `document_id => "%{[@metadata][fingerprint]}"` avec `action => "create"` : une ligne déjà présente est rejetée en conflit (409) au lieu d'être dupliquée.

---

## Partie 4 - Enquête dans Kibana

### 4.1 - Vue d'ensemble
```text
Répartition par code http : 
200 : 17.8k
201 : 1.49k
304 : 488
404 : 508
500 : 5
503 : 402

Répartition par méthode : 
GET : 19.21k
POST : 1.49k

Volume de requête par jour : 
Septembre
23 : 2832
24 : 2884
25 : 2843
26 : 3122
27 : 2903
28 : 3274
29 : 2842

Moyenne = 2957,14

```

### 4.2 - L'incident
1. **Jour et créneau.** Repérer d'abord le jour, puis le créneau :
```text
Jour = 28 septembre : 402 code 500.
Heure : 14h

```
2. **URL touchées et épargnées.** 
URL touchées : 
/api/offres?ville=Toulouse&page=1
erreurs:
46

/api/offres?ville=Montpellier&page=1
erreurs:
48

/api/offres?ville=Nantes&page=1
erreurs:
53

/api/offres?ville=Paris&page=1
erreurs:
54

/api/offres?ville=Lille&page=1
erreurs:
61

/api/offres?ville=Lyon&page=1
erreurs:
67

/api/offres?ville=Bordeaux&page=1
erreurs:
71

URL épargnées : 
/api/offres?ville=Bordeaux&page=2
erreurs:
1

/api/offres?ville=Montpellier&page=4
erreurs:
1

/api/offres?ville=Nantes&page=3
erreurs:
1

/api/offres?ville=Toulouse&page=3
erreurs:
1

3. **Nombre de réponses en erreur et durée.** 
404 erreurs sur 45 minutes.

4. **Comportement des clients.** 
On constate une augmentation du volume des requêtes lors de l'incident, ce qui laisse à penser que les utilisateurs essaient de recharger la page.

### 4.3 - L'activité suspecte
1. **IP de la rafale de 404.**  
IP : 203.0.113.66 avec 300 erreurs.

2. **Moment et durée.** 
26 septembre
13h12 - 13h16

3. **URL demandées.** 
/.env
n: 53

/admin
n: 59

/wp-login.php
n: 43

/.git/config
n: 55

/server-status
n: 44

/phpmyadmin/
n: 46

Le robot cherche sans doute des identifiants publiés accidentellement.

4. **`user_agent.original`.** 
Mozilla/5.0 zgrab/0.x

Cela ressemble à un navigateur mais avec beaucoup moins d'informations que d'habitude.

**Les autres 404 :** 
Les autres 404 semblent venir de navigateurs normaux.

Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36
n: 533

Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15
n: 582

Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36
n: 561

Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0
n: 508

Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1
n: 568

### 4.4 - Offres les plus consultées
OFF-04296
vues: 4

OFF-04033
vues: 3

OFF-03126
vues: 3

OFF-04901
vues: 3

OFF-03532
vues: 3

OFF-04295
vues: 3

OFF-02176
vues: 3

OFF-04271
vues: 3

OFF-00544
vues: 2

OFF-01447
vues: 2
Puis, **en une seule requête Dev Tools**, récupérer les 10 offres dans l'index `offres` :

```
{
...
      {
        "_index": "offres",
        "_id": "OFF-00544",
        "_score": 1,
        "_source": {
          "ville": "Nantes",
          "contrat": "Stage",
          "titre": "Développeur Python (Stage)"
        }
      },
      {
        "_index": "offres",
        "_id": "OFF-01447",
        "_score": 1,
        "_source": {
          "ville": "Nice",
          "contrat": "Stage",
          "titre": "Développeur Front-end (Stage)"
        }
      },
      {
        "_index": "offres",
        "_id": "OFF-03126",
        "_score": 1,
        "_source": {
          "ville": "Lyon",
          "contrat": "CDI",
          "titre": "Administrateur Bases de Données Junior"
        }
      },
      {
        "_index": "offres",
        "_id": "OFF-04901",
        "_score": 1,
        "_source": {
          "ville": "Lyon",
          "contrat": "CDI",
          "titre": "Architecte Cloud Lead"
        }
      },
      {
        "_index": "offres",
        "_id": "OFF-04295",
        "_score": 1,
        "_source": {
          "ville": "Paris",
          "contrat": "Alternance",
          "titre": "Administrateur Bases de Données (Alternance)"
        }
      },
      {
        "_index": "offres",
        "_id": "OFF-04033",
        "_score": 1,
        "_source": {
          "ville": "Paris",
          "contrat": "CDI",
          "titre": "Développeur Python Senior"
        }
      },
      {
        "_index": "offres",
        "_id": "OFF-04296",
        "_score": 1,
        "_source": {
          "ville": "Toulouse",
          "contrat": "CDI",
          "titre": "Analyste Cybersécurité Senior"
        }
      },
      {
        "_index": "offres",
        "_id": "OFF-02176",
        "_score": 1,
        "_source": {
          "ville": "Paris",
          "contrat": "Stage",
          "titre": "Développeur Front-end (Stage)"
        }
      },
      {
        "_index": "offres",
        "_id": "OFF-04271",
        "_score": 1,
        "_source": {
          "ville": "Marseille",
          "contrat": "CDD",
          "titre": "Data Engineer Lead"
        }
      },
      {
        "_index": "offres",
        "_id": "OFF-03532",
        "_score": 1,
        "_source": {
          "ville": "Nantes",
          "contrat": "Alternance",
          "titre": "Ingénieur DevOps (Alternance)"
        }
      }
}
```

### 4.5 - Le public
Mac OS X
n: 582

iOS
n: 568

Windows
n: 561

Android
n: 533

Linux
n: 508

Other
n: 300

TOTAL = 3052
Part des appareils mobiles = 36%

3 navigateurs les + utilisés :
Safari
n: 582

Mobile Safari
n: 568

Chrome
n: 561

---
