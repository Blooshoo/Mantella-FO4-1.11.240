# TopicInfoPatcher
F4SE plugin used by Mantella for subtittle display
Provides a Papyrus function that allows Mantella to properly display subtitles in-game.
Patches the placeholder text entries from Mantella.esp at runtime by overwriting the TopicInfo Responses in memory with the proper text.

If for some reason you want to compile this, you will need to install CommonLibF4 first.
You will need a recent version of Visual Studio. (I used 2022 community)

Alantse has the most up-to-date version of CommonLibF4 as of July 2024 : https://github.com/alandtse/CommonLibF4

Compile CommonlibF4.
Clone or unzip the source code into the CommonlibF4 directory.
Edit CMakelists.txt to add the following line at the bottom:

conditionally_add_subdirectory(TopicInfoPatcher)

Save. After cmake refreshes, you should be able to build TopicInfoPatcher from the Cmake targets view.



