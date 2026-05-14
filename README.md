<h1 align="center">
Surgical Video Annotation Software
</h1>

<h1 align="center">
  <img src="surgui.png">
</h1>

## Description

A graphical video annotation tool, originally developed for labeling surgical videos. It is an easy to use software for labeling videos by creating customizable panels for:

- time-stamping the beginning and end of segments such as activities/tasks
- rating the videos/segments based on a custom scoring system
- extracting and saving important frames such as critical views
- pixel-level annotation of the images by opening in [labelme](https://github.com/wkentaro/labelme/) software

## Installation for the DISE project (verified on Mac)

- Download the SurgUI-master zipped repository [here](https://github.com/wzkariampuzha/SurgUI/archive/refs/heads/master.zip)
- Unzip it
- Download \& install Anaconda
  - [Anaconda](https://www.anaconda.com/download/success)
  - Open the Anaconda-Navigator app
  - Update Anaconda-Navigator if necessary
- Download \& install VLC media player
  - [Mac install (Universal Binary)](https://get.videolan.org/vlc/3.0.21/macosx/vlc-3.0.21-universal.dmg)
  - [Windows install](https://apps.microsoft.com/detail/xpdm1zw6815mqm?ocid=webpdpshare)

- Open the terminal app (Mac) or Windows powershell:
  - Now you need to navigate to the folder where you copied the SurgUI-master folder
  - Eg. If you copied the SurgUI-master folder to `C:\Users\SurgUI-master` then in the command line write `cd C:\Users\SurgUI-master`. To copy the pathname of your SurgUI folder on macOS, see [here](https://support.apple.com/en-gb/guide/mac-help/mchlp1774/mac#:~:text=Show%20the%20path%20to%20a,show%20the%20path%20bar%20momentarily.).
  - See this example:
  - <img src="change-working-directory-example.png">
  - ````shell
        cd path/to/SurgUI-master
        ```
    Then run these commands:
    ````
  - ````shell
        conda env create --file environment.yml
        conda activate surgui-venv
        ```
    Install the software:
    ````
  - ```shell
      python setup.py install
    ```

## Usage

- Open VLC media player

- Open terminal and run the following command:
  ```shell
  conda activate surgui-venv
  surgui
  ```

#### Segment labeling:

For annotating the start and end of segments, click File then "Add Rating Panel" then select the `rating-labels.txt` file containing the list of segments. In the software, create a timestamping panel by opening the text file. For each video that is playing, a folder with the same name is created in “outputs” directory. By pressing the save button, for each segment, a line will be added to a file in the output folder.

Add the video file to the software by clicking the “Add Video” button. The video will start playing in the VLC media player.

#### Saved Annotations

The saved images and the .json files from labelme annotations are stored in `images` folder for each video.
