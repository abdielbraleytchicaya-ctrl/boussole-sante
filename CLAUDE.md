# Boussole santé — Contexte du projet

## Vision
Application d'orientation santé pour le Québec : un « GPS de la santé » qui part
des besoins de la personne (et non de la liste d'hôpitaux) pour l'orienter vers
la bonne ressource — pharmacien, 811/GAP, clinique, ou urgence — avec les temps
d'attente en temps réel et, à terme, la prédiction de l'achalandage. Objectif :
se démarquer des apps existantes (Doctr, Index Santé) qui ne font qu'afficher
des chiffres.

## État actuel (phase 1)
`boussole.py` : script Python autonome (stdlib seulement, Python 3.8+) qui
télécharge les données horaires du MSSS, génère `boussole_sante.html` et
l'ouvre dans le navigateur. Fonctionnalités : liste des urgences par région,
taux d'occupation des civières, personnes en attente, tris/filtres/recherche
(insensible aux accents et aux abréviations St/Ste), cache 10 min, mode
`--demo`, bascule automatique en démo si les sources échouent.

## Sources de données (dans cet ordre)
1. CSV MSSS (mis à jour chaque heure, licence CC-BY 4.0) :
   - https://www.msss.gouv.qc.ca/professionnels/statistiques/documents/urgences/Releve_horaire_urgences_7jours.csv
   - https://www.msss.gouv.qc.ca/professionnels/statistiques/documents/urgences/Releve_horaire_urgences_7jours_nbpers.csv
2. Repli — API datastore CKAN de Données Québec :
   - resource_id civières : a9272cc9-8234-40d1-9806-9f6b4c75c20d
   - resource_id personnes : b256f87f-40ec-4c79-bdba-a23e9c50e741
   - https://www.donneesquebec.ca/recherche/api/3/action/datastore_search?resource_id=...

## Problème n° 1 — RÉGLÉ (2 août 2026)
Cause : le Python de python.org sur macOS n'utilise pas le trousseau du système,
il attend son propre fichier de certificats installé par « Install
Certificates.command ». Ce script n'ayant jamais été lancé, Python avait zéro
autorité de certification (`ssl.create_default_context().get_ca_certs()` → 0) et
*toute* connexion HTTPS échouait avec `CERTIFICATE_VERIFY_FAILED`. Ce n'était ni
un délai dépassé ni un blocage anti-robot.
Correctif : `ssl_context()` dans `boussole.py` cherche un vrai jeu de
certificats dans cet ordre — configuration Python, paquet `certifi`, puis les
fichiers système (`/etc/ssl/cert.pem`, etc.) — et le passe aux deux `urlopen`.
La vérification TLS n'est jamais désactivée. Si rien n'est trouvé, message
d'erreur en français indiquant de lancer « Install Certificates.command ».
Résultat vérifié : 107 installations, « données MSSS en direct ».

## Particularités connues des données
- 120 installations dans les fichiers, dont 13 sans données (régions
  suspendues) → 107 affichées.
- Le numéro de permis N'EST PAS une clé de fusion fiable : le MSSS n'inscrit pas
  le même numéro dans les deux fichiers pour ~29 installations. La fusion se
  fait sur le nom normalisé de l'installation (concordance 120/120).
- Le fichier `_nbpers` contient toutes les colonnes du premier plus les
  personnes présentes, l'attente et la région officielle (colonne `Region`,
  utilisée en priorité pour le regroupement).
- Le fichier `_nbpers` se termine par 17 lignes de totaux (« Total régional »,
  « Ensemble du Québec ») à écarter — voir `AGREGATS`.
- L'URL `www.msss.gouv.qc.ca` redirige (301) vers `msss.gouv.qc.ca` : normal,
  `urllib` suit la redirection.
- Diffusion actuellement suspendue par le MSSS pour deux régions : CIUSSS de la
  Mauricie–Centre-du-Québec (dont Hôpital Sainte-Croix, Drummondville) et
  CIUSSS du Nord-de-l'Île-de-Montréal. Ne pas « corriger » leur absence.
- Encodage variable (UTF-8/Latin-1), séparateur ; ou , décimales à virgule —
  le parseur détecte les colonnes par mots-clés normalisés, conserver cette
  tolérance.
- Écarts de quelques unités avec Québec.ca possibles (décalage horaire de
  publication + cache) : normal.

## Feuille de route
1. ~~Corriger l'accès aux données réelles.~~ Fait.
2. ~~Historiser chaque relevé horaire dans SQLite.~~ Fait (2 août 2026) :
   `boussole_historique.db`, table `releves`, clé primaire
   (releve, installation) où `releve` est l'horodatage « Mise_a_jour » du
   fichier MSSS → relancer le script dans la même heure n'ajoute rien
   (INSERT OR IGNORE). Le mode `--demo` n'écrit jamais dans la base, et un
   pépin SQLite n'empêche jamais la page de s'afficher. Index
   (installation, releve) prêt pour les requêtes de tendances.
   Collecte automatique en place (2 août 2026) : tâche launchd
   `com.boussole.collecte` (~/Library/LaunchAgents/com.boussole.collecte.plist)
   qui lance `python3 boussole.py --collecte` chaque heure à h:55 (le MSSS
   publie vers h:46) et à l'ouverture de session (rattrapage sans doublons).
   Le mode `--collecte` n'ouvre pas le navigateur, n'écrase pas la page si la
   source échoue (sortie 1), et journalise dans boussole_collecte.log.
   Désactiver : `launchctl bootout gui/$(id -u)/com.boussole.collecte`.
   Attention : le plist contient des chemins absolus — à refaire si le projet
   ou Python change d'emplacement. launchd ne collecte pas quand le Mac est
   éteint ou en veille (rattrapage au réveil/à l'ouverture de session).
3. ~~Géolocalisation + temps de trajet → tri par « temps total ».~~ Fait
   (2 août 2026). Coordonnées des installations : répertoire cartographique
   M02 du MSSS sur Données Québec (resource_id `2aa06e66-…`), jointure par
   permis puis par nom normalisé (couverture 120/120), cache local 30 jours
   dans `boussole_coords.json`. Position de l'utilisateur : API de
   géolocalisation du navigateur, avec repli sur une liste de ~30 villes si
   refusée — tout le calcul (haversine, estimation du temps de route
   30-80 km/h sur distance × 1,3, temps total = trajet + DMS ambulatoire)
   est fait en JavaScript local, la position ne quitte JAMAIS le navigateur
   (Loi 25). Limite connue et affichée : distances à vol d'oiseau — le
   trajet réel peut être bien plus long (fleuve, traversiers). Un vrai
   calcul d'itinéraire attendra une phase ultérieure.
4. ~~Tendances par hôpital.~~ Fait (2 août 2026), en mode progressif :
   `load_trends()` calcule la moyenne des personnes en attente par heure de
   la journée (extraction de l'heure depuis l'horodatage `releve`, positions
   12-13) ; le payload porte `trend` (24 cases) par installation et `tjours`
   (jours distincts). L'interface n'affiche rien avant 3 jours de collecte
   (note explicative à la place) puis, par carte : mini-graphique 24 barres
   (heure actuelle en orange, infobulles) et phrase « l'attente baisse/monte
   habituellement vers X h » (comparaison heure actuelle vs 6 prochaines
   heures, seuils ±20 %/25 %). Jamais de tendances en mode démo. Testé avec
   une base synthétique de 7 jours dans le scratchpad (jamais dans la vraie
   base).
5. ~~Questionnaire d'orientation informatif.~~ Fait (2 août 2026), sous forme
   de « guide des ressources » replié en haut de page. Principe directeur
   pour rester informatif et non diagnostique : AUCUNE question sur des
   symptômes, aucun calcul de gravité — la personne choisit elle-même la
   situation qui lui ressemble parmi des libellés de besoins, et le guide
   décrit factuellement chaque ressource publique (pharmacien, 811
   Info-Santé/Info-Social, GAP 811 option 3, clinique/RVSQ/CLSC, urgence).
   Encadré 911 (signes d'alarme + 988) toujours affiché en tête. Aucun
   stockage (ni localStorage ni réseau), remise à zéro à chaque ouverture.
   Le résultat « urgence » renvoie à la liste avec tri « temps total ».
   IMPORTANT avant toute diffusion publique : faire valider les textes par
   un professionnel de la santé (prévu au README depuis le début).
6. ~~PWA hébergée.~~ Fait côté code (2 août 2026), activation en attente :
   `--site` génère site/ (index.html qui charge donnees.json par fetch,
   manifeste, service worker réseau-d'abord/cache-en-secours, icônes PNG
   générées en pur Python) ; refuse de publier si la source réelle échoue
   (exit 1 → l'ancienne version reste en ligne). `--servir` sert site/ sur
   localhost:8765 pour tester (SW exige HTTPS ou localhost — testé : SW
   actif, hors-ligne fonctionnel). Hébergement prévu : GitHub Actions
   (cron h:55 UTC, historique SQLite dans le cache d'Actions — si perdu,
   les tendances repartent après 3 jours) + GitHub Pages ; workflow prêt
   dans .github/workflows/collecte.yml, .gitignore prêt (site/ et fichiers
   générés exclus, boussole_coords.json versionné comme secours).
   ACTIVATION = décision de l'utilisateur : créer le dépôt GitHub, pousser,
   régler Pages sur « GitHub Actions ». RAPPEL : diffusion publique →
   validation professionnelle des textes du guide d'abord (item 1 README).
   La page autonome (double-clic) et le mode --demo restent inchangés.
   État au 2 août 2026 : dépôt git LOCAL initialisé (branche main, premier
   commit fait), gh CLI authentifié (compte abdielbraleytchicaya-ctrl,
   forfait vraisemblablement gratuit → Pages exige un dépôt public).
   L'utilisateur a choisi de NE PAS héberger pour l'instant : attendre la
   validation professionnelle des textes. Quand ce sera fait, les 3
   commandes (gh repo create --public --push, activation Pages
   build_type=workflow, gh workflow run) sont dans l'historique de
   conversation et dans le README. Rien n'a été publié.

## Ajouts après la feuille de route initiale
- Tri « distance » + bascule automatique en « plus proche d'abord » dès que
  la position est activée (le choix « temps total » explicite est respecté).
- Fiche détaillée par hôpital (clic sur une carte) : chiffres du moment en
  langage clair, attente typique, distance/temps, adresse (répertoire M02,
  cache v2 avec [lat, lon, adresse]), bouton Itinéraire (Google Maps,
  destination seulement — la position de l'utilisateur n'y est pas mise),
  tendance 24 h, rappels 811/911 et « cas prioritaires vus en premier ».
  Fermeture par ✕, Échap ou clic à l'extérieur.
- Section « Si vous y allez maintenant » dans la fiche : fourchette de temps
  sur place (DMS ambulatoire d'hier × facteur d'achalandage borné 0,6-1,8 =
  attente actuelle / moyenne typique de l'heure, fourchette ±25 %), indice
  « plus calme / normal / plus occupé que d'habitude », et « Quand partir ? »
  (heure la plus calme des 12 prochaines heures d'après le profil historique,
  proposée seulement si ≤ 75 % de la moyenne actuelle). Sans 3 jours
  d'historique : fourchette simple + note d'attente. JAMAIS présenté comme
  une promesse — toujours « estimation indicative », rappel que la priorité
  au triage domine. Ne pas transformer en « prédiction » ferme : c'est la
  ligne à ne pas franchir (instrument médical + honnêteté).

## Refonte v2 (2 août 2026) — trois écrans
- Accueil orienté besoin : « Que cherchez-vous ? » avec 3 tuiles (soins
  maintenant → tri DISTANCE + localisation — changé le 2 août soir : le
  temps total déroutait, « position activée » doit montrer le plus proche
  d'abord ; le tri temps total reste un choix explicite du menu, y compris
  pour le bouton « voir les urgences » du guide qui est passé à distance
  aussi ; guide ; liste en direct),
  bandeau 911/988/811, pastilles d'en-tête (fraîcheur du relevé « il y a
  X min », nombre d'urgences, position — cliquable). Clic sur le titre =
  retour accueil. La liste ne s'affiche qu'après un choix.
- Cartes allégées : nom, distance (ou badge %), une ligne (fourchette de
  temps sur place + attente), jauge d'achalandage colorée. L'indice de la
  jauge = moyenne de (taux civières %) et (pression d'attente : 2 personnes
  en attente par civière fonctionnelle = 100 %) ; seuils <85 fluide,
  85-124 chargé, ≥125 débordé. Légende au-dessus de la liste. Le graphique
  de tendance ne vit plus que dans la fiche.
- Fiche : blocs colorés « Si vous y allez maintenant » (bleu) et « Quand
  partir ? » (vert), bouton Favori. Favoris = noms d'hôpitaux en
  localStorage (aucune donnée de santé), épinglés en groupe « ★ Mes
  favoris » dans la vue par région, étoile sur les cartes.
- Mode sombre (2 août 2026) : palette complète via variables CSS +
  `prefers-color-scheme: dark` ; aucune couleur codée en dur dans le JS ;
  la PWA a deux balises theme-color (clair/sombre). Les couleurs de jauge
  (tons moyens) servent telles quelles dans les deux modes.
- CLSC proches dans le guide (2 août 2026) : le cache M02 (v3) garde la
  liste des 508 points de service CLSC (nom, adresse, lat/lon) ; les
  résultats « clinique » et « GAP » du guide affichent les 3 plus proches
  avec distance et itinéraire, calculés localement. Sans position : astuce
  pour l'activer, et rafraîchissement automatique quand elle arrive.
  Avertissement affiché : les services varient par point de service,
  appeler avant de se déplacer.

- Carte géographique (2 août 2026) : bouton Liste/Carte dans la barre.
  SVG généré en JS depuis les lat/lon (projection équirectangulaire avec
  correction cos(lat)) — AUCUNE tuile externe, tout reste local. Cadre par
  défaut = sud habité (44.95-50.55 / -79.9 à -61.2), bouton « Voir tout le
  Québec » pour le Nord ; un filtre région/recherche recadre
  automatiquement sur les points filtrés. Points colorés par l'indice de
  la jauge, clic = fiche, anneau bleu = position de l'utilisateur.

- Comparateur (2 août 2026) : lien « + Comparer » sur chaque carte (max 3,
  sélection en mémoire seulement), barre flottante en bas, tableau côte à
  côte dans un voile : occupation, attente, présents, 24 h+, fourchette de
  temps sur place, séjour moyen, et distance/route/temps total si position.
  Meilleure valeur de chaque ligne surlignée (minimum ; égalités = toutes
  surlignées ; lignes vides masquées). Boutons « Fiche » qui enchaînent
  sur la fiche détaillée. Rappels habituels en pied de tableau.

- Historique réel 24-48 h (2 août 2026) : `load_recent()` envoie les 48
  derniers relevés (format compact : liste d'horodatages `hrel` partagée +
  valeurs alignées par installation dans `d.histo`). Dans la fiche, section
  « Dernières heures » : barres bleues des relevés réels (infobulles avec
  « la veille » quand la date diffère, dernière barre foncée) + phrase de
  tendance (comparaison dernier relevé vs ~3 h avant : hausse/baisse si
  écart ≥ 3 personnes ET ≥ 20 %, sinon stable). Affichée dès 3 relevés.
  Distincte du profil « typique » gris (moyennes par heure).

- Résumé provincial (2 août 2026) : bloc « En ce moment au Québec » sur
  l'accueil — occupation moyenne pondérée (Σ occupées / Σ fonctionnelles,
  PAS une moyenne de %), personnes dans les urgences, en attente d'un
  médecin, nombre d'urgences en débordement (indice ≥ 125). Top 3 des
  régions par taux moyen (≥ 2 hôpitaux diffusés), cliquables → liste
  filtrée sur la région. Mention « sur les N urgences diffusées ». Masqué
  en mode démo.

- Page méthodologie (2 août 2026) : lien « Méthodologie, sources et vie
  privée » au pied de page → panneau (même voile que la fiche). Contenu
  dans le div caché #methodo-src du gabarit HTML (accents non échappés).
  5 sections : sources, signification des chiffres, calculs (jauge,
  fourchette, quand partir, distances, tendances, résumé pondéré), ce que
  l'outil ne fait pas, vie privée. À maintenir en phase avec les calculs :
  toute modification d'une formule doit être répercutée ici.

- Ville mémorisée (2 août 2026) : quand l'utilisateur choisit sa ville dans
  la liste de repli, elle est retenue en localStorage (`bousville`, appareil
  seulement). À l'ouverture suivante : position active automatiquement,
  pastille « 📍 Ville ». Le choix de ville se fait dans une FENÊTRE
  par-dessus l'écran (`ouvrirChoixVille`) — pas dans le sélecteur de la
  barre, invisible depuis l'accueil (bug corrigé le 2 août soir : cliquer
  « Activer ma position » depuis l'accueil ne montrait rien quand le GPS
  échouait). Clic sur la pastille : GPS si pas de position, sinon fenêtre
  de changement de ville. Ordre de `locate()` : GPS → ville mémorisée →
  fenêtre de choix. La position GPS, elle, n'est JAMAIS
  enregistrée. Textes de vie privée (note + méthodologie) mis à jour en
  conséquence. Pas de géolocalisation par IP (refusée : l'IP partirait vers
  un tiers, contraire à la promesse de confidentialité).

## Refonte v3 (2 août 2026, soir) — maquette « parcours urgences »
Implémentation de la maquette Claude Design remise par l'utilisateur
(~/Downloads/maquette-parcours-urgences/). Trois écrans avec onglets
« 1 · Orienter / 2 · Choisir / 3 · Y aller » :
- Écran 1 : héros « Où aller, maintenant ? », triage 3 cartes (911 rouge /
  811+guide / « Besoin de voir quelqu'un » bleu → écran 2), ligne 988,
  carte « Recommandé près de vous » (meilleur temps total si position,
  sinon bouton d'activation) et carte « Le réseau en ce moment »
  (décompte fluide/chargé/débordé par indice, moyenne pondérée, attente
  totale, top régions cliquables, fraîcheur mono).
- Écran 2 : barre en pilules, ligne « N lieux · triés par X » + légende à
  points, cartes de liste façon maquette (type · km en mono, nom, grosse
  fourchette colorée à droite, jauge, état · occupation, Détails → /
  + Comparer). Carte SVG et comparateur inchangés.
- Écran 3 : la fiche est un ÉCRAN (plus un voile) — retour, tuiles stats
  (fourchette en tuile teintée brand), « Ce qui vous attend » (chronologie
  triage/attente/sortie), estimation + quand partir + dernières heures +
  affluence par heure (12 barres bicolores, creux/pointe), colonne droite :
  itinéraire (pilule pleine), favori, « À apporter », note source.
- Identité : logo réel (rosace extraite et réduite en pur Python →
  logo_rosace.png, embarquée en data-URI via __LOGO__), bleu #16468C,
  chiffres en mono système, fond papier. PAS de Google Fonts (vie privée) —
  polices système. Adaptations honnêtes vs maquette : fourchettes au lieu
  de chiffres uniques, pas de filtres 24h/pédiatrie (données absentes),
  pas d'alerte « si l'attente baisse » (pas de serveur), CLSC au lieu de
  « plages libres en clinique ».
- Le guide vit en overlay (openGuide, contenu dans #guide-src) ; voiles
  conservés pour guide/méthodo/comparateur/choix de ville.
- Urgences spécialisées (2 août soir, signalé par l'utilisateur : l'IUSMM
  était « recommandé » depuis Longueuil) : vocation() détecte par nom
  (SANTÉ MENTALE/DOUGLAS ; CARDIOLOGIE/PNEUMOLOGIE ; SAINTE-JUSTINE/POUR
  ENFANTS — attention, PAS le mot « enfant » seul : Enfant-Jésus est un
  hôpital général). Étiquetées dans la liste (« Urgence santé mentale »)
  et la fiche, exclues de « Recommandé près de vous » (meilleurGeneral()),
  note affichée si une spécialisée aurait été première. Documenté dans la
  méthodologie.
- Recommandation avec garde-fous (2 août soir, signalé par l'utilisateur :
  Coaticook à 105 km « recommandé » depuis Saint-Hyacinthe pour ~20 min de
  gain) : meilleurGeneral() limite les candidats au rayon (plus proche
  + 60 km, min 80 km) et n'accepte un hôpital plus loin que le plus proche
  que si le gain de temps total ≥ 45 min (sinon → le plus proche). La
  phrase de la carte explique le choix (« le plus proche… » ou « ≈ X de
  moins que Y, pourtant plus proche »). Règle documentée dans la
  méthodologie. Seuils : 60/80 km et 45 min — cohérents avec la fourchette
  ±25 % des estimations.
- PIÈGE d'init corrigé : reprendreVille() doit être appelé APRÈS
  l'attachement des écouteurs (il remplace #loc par le sélecteur de ville,
  et un addEventListener sur un élément disparu tuait toute l'init).
- PIÈGE d'échappement : dans PAGE, les emojis \\uD83D... doivent avoir un
  DOUBLE antislash, sinon Python crée des demi-surrogates invalides.

## Contraintes non négociables
- Rester informatif, jamais diagnostique (éviter le statut d'instrument médical
  de Santé Canada) : orientation vers des ressources, renvoi systématique au
  911/811 pour tout signal d'alarme.
- Vie privée (Loi 25) : aucun compte, aucune donnée de santé stockée, triage
  local à l'appareil.
- Toujours afficher : source MSSS/Données Québec, « les cas prioritaires sont
  vus en premier », et l'avertissement 911.
- Interface en français d'abord.

## Conventions techniques
- Python stdlib uniquement pour la phase 1 (aucune dépendance à installer).
- L'utilisateur est débutant en programmation : expliquer les changements
  simplement, tester avant de livrer, messages d'erreur clairs en français.
