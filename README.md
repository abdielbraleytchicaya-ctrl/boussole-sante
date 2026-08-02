# Boussole santé — Prototype phase 1

Tableau de bord de l'état des urgences du Québec, alimenté par les données
ouvertes du ministère de la Santé et des Services sociaux (MSSS), publiées
chaque heure sur Données Québec.

## Démarrage (2 minutes)

Prérequis : Python 3.8 ou plus récent (déjà installé sur macOS et Linux ;
sur Windows, l'installer depuis python.org).

```
python3 boussole.py
```

La page `boussole_sante.html` est générée puis s'ouvre toute seule dans le
navigateur.

Si le réseau bloque l'accès au site du MSSS (certains réseaux d'entreprise
ou serveurs infonuagiques), lancer le mode démonstration pour voir
l'interface avec des données fictives :

```
python3 boussole.py --demo
```

## Ce que fait le prototype

L'application s'ouvre sur un accueil « Que cherchez-vous ? » : besoin de
soins maintenant, guide d'orientation, ou état des urgences en direct. La
liste montre des cartes simples (temps sur place estimé, personnes en
attente, jauge d'achalandage) ; le détail complet vit dans la fiche de
chaque hôpital. On peut épingler ses hôpitaux en favoris (conservés
uniquement sur l'appareil).

- Télécharge et fusionne les deux fichiers officiels du MSSS :
  - Releve_horaire_urgences_7jours.csv (civières fonctionnelles/occupées, 24 h+, 48 h+)
  - Releve_horaire_urgences_7jours_nbpers.csv (personnes présentes, en attente
    d'un médecin, durées moyennes de séjour)
- Calcule le taux d'occupation des civières par installation
- Regroupe par région, avec recherche, filtre et tri (occupation, attente, séjour moyen)
- Met les données en cache 10 minutes (elles changent chaque heure à la source)
- Bascule automatiquement en mode démonstration si la source est inaccessible
- Fiche détaillée au clic sur un hôpital : les chiffres du moment expliqués
  simplement, l'attente typique, la distance, l'adresse et un bouton
  itinéraire.
- « Si vous y allez maintenant » : une fourchette de temps sur place estimée
  (cas non prioritaires), un indice « plus calme / plus occupé que
  d'habitude », et — après 3 jours de collecte — le meilleur moment pour
  partir d'ici 12 h. Estimations indicatives : la priorité au triage passe
  toujours en premier.
- Guide « Où aller ? » : un petit parcours informatif qui aide à choisir
  entre pharmacien, 811, GAP, clinique et urgence. Il ne pose aucune
  question de santé et n'évalue rien : on choisit soi-même sa situation,
  le guide décrit ce que chaque ressource offre. Avec la position activée,
  les résultats « clinique » et « GAP » montrent les 3 CLSC les plus
  proches (sur les 508 du Québec) avec adresse et itinéraire. Signes
  d'alarme 911 et ligne 988 toujours affichés. Rien n'est enregistré ni
  transmis. À faire valider par un professionnel de la santé avant
  diffusion publique.
- Mode sombre automatique, selon le réglage de l'appareil.
- Vue carte : les urgences sur une carte du Québec, colorées selon
  l'achalandage, dessinée localement (aucun service de cartographie
  externe). Un point = un hôpital ; toucher un point ouvre sa fiche.
- Comparateur : « + Comparer » sur 2 ou 3 hôpitaux, puis un tableau côte
  à côte avec la meilleure valeur de chaque ligne surlignée.
- Dans la fiche, « Dernières heures » : la courbe réelle des dernières
  24-48 h (relevés observés) avec une phrase de tendance — « en hausse
  depuis 14 h », « en baisse », « plutôt stable ».
- Sur l'accueil, « En ce moment au Québec » : occupation moyenne,
  personnes dans les urgences, en attente, urgences en débordement, et
  les trois régions les plus chargées (cliquables).
- « Méthodologie, sources et vie privée » (lien au pied de page) : d'où
  viennent les données, ce que veulent dire les chiffres, comment chaque
  estimation est calculée, ce que l'outil ne fait pas.
- Tendances par hôpital (après 3 jours de collecte) : mini-graphique de
  l'attente typique heure par heure et phrase du type « l'attente baisse
  habituellement vers 21 h », calculés à partir de l'historique local.
- Bouton « Autour de moi » : distances, temps de route estimés et tri par
  « temps total » (trajet + séjour moyen). La position est traitée
  uniquement dans le navigateur — jamais enregistrée ni transmise. Si la
  géolocalisation est refusée, on peut choisir sa ville dans une liste.
  Les coordonnées des hôpitaux viennent du répertoire cartographique M02
  du MSSS (cache local de 30 jours dans `boussole_coords.json`).
- Conserve chaque relevé horaire dans `boussole_historique.db` (SQLite,
  créée automatiquement à côté du script) : c'est la matière première des
  futures tendances et prédictions. Relancer le script plusieurs fois dans
  la même heure n'ajoute pas de doublons, et le mode démonstration n'écrit
  jamais dans la base.

## Collecte automatique (macOS)

Une tâche `launchd` (`com.boussole.collecte`) lance
`python3 boussole.py --collecte` chaque heure à h:55 et à l'ouverture de
session : l'historique se remplit tout seul, sans ouvrir de navigateur.
Le journal s'écrit dans `boussole_collecte.log`, à côté du script.

- Vérifier qu'elle tourne : `launchctl list | grep boussole`
- La désactiver : `launchctl bootout gui/$(id -u)/com.boussole.collecte`
- La réactiver :
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.boussole.collecte.plist`

Le Mac ne collecte pas quand il est éteint ou en veille ; il rattrape le
relevé courant au réveil. Si le dossier du projet change d'emplacement,
refaire le fichier `~/Library/LaunchAgents/com.boussole.collecte.plist`
(il contient des chemins absolus).

Le parseur détecte les colonnes par mots-clés et gère les variations
d'encodage (UTF-8/Latin-1) et de séparateur ( ; ou , ), pour résister aux
petits changements de format du fichier source.

## PWA (application installable)

```
python3 boussole.py --servir
```

Génère le site complet dans `site/` et le sert sur http://localhost:8765 :
la Boussole devient une application installable (icône, plein écran, mode
hors ligne avec les dernières données vues). `python3 boussole.py --site`
génère le dossier sans le servir (c'est ce que fait l'hébergeur).

### Hébergement (GitHub Pages — gratuit)

Tout est prêt dans le dépôt (`.github/workflows/collecte.yml`) : chaque
heure, GitHub Actions exécute la collecte et publie le site — le Mac n'a
plus besoin d'être allumé. Pour activer :

1. Créer un dépôt public sur github.com et y pousser ce dossier ;
2. Dans le dépôt : Settings → Pages → Source : « GitHub Actions » ;
3. Attendre l'exécution suivante (ou Actions → « Collecte et publication »
   → Run workflow). L'adresse sera `https://<compte>.github.io/<depot>/`.

Notes : le workflow ne publie jamais la démo (si le MSSS est inaccessible,
la version précédente reste en ligne) ; l'historique des tendances vit
dans le cache d'Actions et repart de zéro s'il est perdu (les tendances
reviennent après 3 jours). Avant de partager l'adresse publiquement,
faire valider les textes du guide (voir feuille de route).

## Prochaines étapes (feuille de route)

1. Faire valider les textes du guide « Où aller ? » par un professionnel
   de la santé (obligatoire avant toute diffusion publique).
2. Activer l'hébergement GitHub Pages (voir la section PWA ci-dessus) une
   fois les textes validés.
3. Itinéraires réels : remplacer l'estimation « à vol d'oiseau » par un
   vrai calcul de trajet (en respectant la vie privée).
4. Mobile natif si l'application PWA gagne des utilisateurs.

## Mentions

Source des données : Fichier horaire de la situation à l'urgence, Console
provinciale des urgences (CPU), MSSS, diffusé sous licence ouverte via
Données Québec. Outil d'information seulement : ne remplace pas un avis
médical. En cas d'urgence vitale, composez le 911.
