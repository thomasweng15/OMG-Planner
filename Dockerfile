FROM nvidia/cuda:11.4.1-cudnn8-devel-ubuntu20.04
#########
# Credit: https://github.com/askforalfred/alfred/blob/master/Dockerfile
#########

ARG USER_NAME
ARG USER_PASSWORD
ARG USER_ID
ARG USER_GID

RUN apt-get update
RUN apt install sudo
RUN useradd -ms /bin/bash $USER_NAME
RUN usermod -aG sudo $USER_NAME
RUN yes $USER_PASSWORD | passwd $USER_NAME

# set uid and gid to match those outside the container
RUN usermod -u $USER_ID $USER_NAME
RUN groupmod -g $USER_GID $USER_NAME

# work directory
WORKDIR /home/$USER_NAME

# install system dependencies
COPY ./docker/install_deps.sh /tmp/install_deps.sh
RUN chmod +x /tmp/install_deps.sh
RUN yes "Y" | /tmp/install_deps.sh

# setup python environment
RUN cd $WORKDIR
ENV VIRTUAL_ENV=/home/$USER_NAME/omg
RUN python3 -m virtualenv --python=/usr/bin/python3 $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# install python requirements
RUN pip install --upgrade pip==21.2.4
RUN pip install -U setuptools
COPY ./requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# install GLX-Gears (for debugging)
RUN apt-get update && apt-get install -y \
   mesa-utils && \
   rm -rf /var/lib/apt/lists/*

# change ownership of everything to our user
 
RUN cd ${USER_HOME_DIR} && echo $(pwd) && chown $USER_NAME:$USER_NAME -R .

# build libraries
ENTRYPOINT bash -c "export OMG_ROOT=~/omg && /bin/bash"