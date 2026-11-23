# Notes de version — F95Checker Enhanced

**[English](../CHANGELOG.md)** · [README](../README.fr.md) · [Guide d'utilisation](GUIDE_UTILISATEUR.md)

## 11.2.1.1

**F95Checker Enhanced 1**, basé sur F95Checker 11.2.1 de WillyJL. Tout ce qui suit est nouveau par rapport à la 11.2.1 officielle.
Tes données F95Checker (`%APPDATA%\f95checker`) sont utilisées telles quelles : aucune nouvelle colonne en base, revenir à la version officielle ne fait rien perdre.

### Installation :
- Le zip Windows contient tous les outils des nouvelles fonctions (Real-ESRGAN, RIFE, ffmpeg, Depth Anything V2 Small, ONNX Runtime DirectML) : décompresser, lancer, rien d'autre à installer
- Les mises à jour depuis l'appli viennent des versions de F95Checker Enhanced

### Scan de la bibliothèque locale (après chaque Refresh complet) :
- Exe trouvés tout seuls : gardés s'ils sont valides, relocalisés si tu as déplacé ou renommé un dossier, trouvés par nom ou par sigle (`HoS` = House of Shinobi) ; meilleur exe choisi (64 bits, pas de crash handler ni d'installeur, Unity et Godot reconnus)
- Un dossier plus récent du même jeu sur tes disques : l'exe bascule dessus
- **Installed** coché / décoché tout seul d'après la version dans le nom des dossiers, ou les dates des fichiers
- Nouvelles colonnes : **Installed On** (arrivée des fichiers de ta version sur le disque, sauvegardes et logs ignorés), **Size On Disk**, **Engine (detected)**, **Cheats**, **Toy**
- **Last Launched** lu dans les sauvegardes et les logs (Ren'Py, RPG Maker, Unity, Unreal) : marche aussi pour les jeux lancés depuis l'Explorateur
- **Delete old versions** : anciens dossiers et copies en double d'un jeu, à la corbeille ou définitivement, sauvegardes copiées avant
- Mods Ren'Py et sauvegardes RPG Maker reportés vers la nouvelle version ; mods plus anciens que la mise à jour signalés (désactiver / garder / chercher une mise à jour)
- Dossiers de bibliothèque déduits de tes exe, sous-dossiers de catégorie gérés ; clic droit > **Scan Local Copy**

### Mises à jour :
- Fenêtre **Updates to do** : les plus récemment joués d'abord, mention FINAL VERSION, changelog depuis ta version, alerte de place disque, Ignore, Download (page de l'hébergeur dans ton navigateur, ou JDownloader), Import
- **Import Update** (.zip / .7z / .rar) : aperçu, extraction sur le même disque, vérification que le jeu n'est pas lancé, sauvegardes copiées, dossier renommé à la nouvelle version, Installed coché, anciens fichiers moteur Ren'Py mis de côté
- **Surveillance des téléchargements** : une archive terminée qui ressemble à un de tes jeux propose **Import into <jeu>**
- Revue des **jeux finis** : marquer fini, désinstaller en option (sauvegardes copiées)

### Bibliothèque :
- Diaporama des captures du thread au survol, étagère **To play** avec badges UPDATE / NEW, bandeau **Continue**, couleurs des jeux
- **Bannières plus nettes** (Real-ESRGAN, originales jamais modifiées), **What to play tonight?**, **Statistiques** (temps de jeu par semaine, développeurs, tags, moteurs, place par disque)
- Barre latérale **Library tools** et fenêtre de réglages

### Outils Ren'Py :
- Console (Maj+O) et menu développeur dans chaque jeu Ren'Py par défaut, désactivables par jeu
- **Fiche de triche** : les variables du jeu lues dans les .rpy / .rpyc / .rpa (rien n'est exécuté), classées (argent, relations, stats, objets, drapeaux), copie de la ligne, recherche par valeur
- **Menu de triche en jeu (Maj+C)** : boutons -10 / -1 / +1 / +10 / MAX et interrupteurs, réécrit après chaque mise à jour
- **Galerie et replays débloqués** (réversible), **retour arrière et avance rapide toujours permis**, préférences communes (passer le texte non lu, avance auto, volumes, mode fenêtre) appliquées une fois par jeu
- **What went wrong?** après un plantage : l'erreur expliquée, le fichier en cause et une correction en un clic quand il y en a une (fichiers moteur mélangés, mod cassé, script en double / « appeared twice », fichiers manquants…)
- **Installer un mod** (archive ou dossier) et le **désinstaller** ensuite ; liens **Mods & extras** des threads ; **Recommandations** d'après tes notes et ton temps de jeu
- **HD & 60 fps** : images HD pour les jeux Ren'Py 8.4+ faits pour moins que ton écran (1080p / 1440p / 2160p), animations x2 images par seconde par RIFE (même durée, synchro du jouet inchangée), annulation par jeu, **Start all** pour la bibliothèque (s'arrête à 15 Go libres par disque)
- **3D & images vivantes** (Ren'Py 8.1+) : cartes de profondeur calculées une fois par la carte graphique, **Maj+P** en jeu : images vivantes (douces, suivent la souris, le sujet reste immobile) / 3D côte à côte pour la VR (Half SBS) / off ; animations comprises
- **Moteur Ren'Py partagé** : fichiers moteur identiques stockés une fois par disque (liens physiques NTFS), annulable
- Onglet **Status** dans la fiche du jeu : tout ce qui a été ajouté ou trouvé pour le jeu, avec son bouton

### Jouet connecté (Intiface Central) :
- Synchro des animations Ren'Py : scripts générés à partir des animations elles-mêmes (analyse du mouvement, format funscript, boucles respectées), tous les jeux sur plusieurs cœurs
- Vibrations de manette (Game Haptics Router), mode piloté par le son, jeux avec support jouet intégré détectés
- Va-et-vient (The Handy, Keon…) qui suivent les positions du script ; motifs avec éditeur ; les .funscript téléchargés passent toujours en priorité
- Sécurité : arrêt d'urgence (Pause / Ctrl+Alt+Fin / STOP TOY), démarrage doux, plafonds, durée maximale, pause quand le jeu n'est pas au premier plan, arrêt à la fermeture

### Disques et ménage :
- **Compression** (LZX de Windows) avec gain estimé, annulable
- **Rééquilibrage des disques** : les jeux joués il y a le plus longtemps déplacés vers le disque le plus vide, copie vérifiée avant de supprimer l'ancien dossier
- **Clean up** (archives déjà installées, caches Ren'Py, restes), **Tidy up** (dossiers de catégorie appris de ton rangement)

### Discrétion :
- **Touche de discrétion** (Ctrl+Alt+H) : arrête le jouet, réduit les jeux et F95Checker, coupe le son
- **Écran sage** (Ctrl+Alt+B) : bannières, noms, tags et captures masqués dans l'interface
- **Verrou par code** (démarrage, inactivité, réduction), titre de fenêtre neutre, option sans aperçu / sans capture

### Affichage :
- **Profil d'affichage** par jeu : Lossless Scaling (agrandissement ou génération d'images), ReShade activé / désactivé au lancement
- Kit DLSS 5 (avancé, uniquement ton propre kit, non fourni) : installer / retirer par jeu, désactivé par défaut
