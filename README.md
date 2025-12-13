**Download youtube videos as mp3 and find cover pictures and albums.**

put one or multiple links at every newline in yt_links.txt file (Create a txt file and name it yt_links.txt in Music folder)

After renaming the music files, follow the instructions printed

If the CLI options failed to fetch cover art:

  For cover art, you must have the title of the track and the contributing artist labelled in properties
  Cover art prompt: 
  
`  python mp3_cover_finder.py -p "Your folder location of all the music files" --recursive`

  Use same_cover.py if you want to apply a single cover art to multiple mp3 files.
  In the same_cover.py, input the location of the cover art and the folder that contains the mp3 files. 

  Use unlink_album_cover.py if you want to unlink a cover art of an mp3 file if it's not to your liking.
