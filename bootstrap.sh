#!/bin/bash

# Check that the environment file exists.
if [ ! -f local/environment.json ]; then
	echo "Missing: local/environment.json"
	exit 1
fi

# DEPLOYED TO WEB ONLY
if [ "$1" == "--deployed" ]; then
	git config --global user.name "Joshua Tauberer"
	git config --global user.email jt@occams.info
	git config --global push.default simple

	sudo apt-get update -q -q && sudo apt-get upgrade
fi

# Get remote libraries.

git submodule update --init

# Install package dependencies.

function apt_install {
	# Check which packages are already installed before attempting to
	# install them. Avoids the need to sudo, which makes testing easier.
	PACKAGES=$@
	TO_INSTALL=""
	for pkg in $PACKAGES; do
		if ! dpkg -s $pkg 2>/dev/null | grep "^Status: install ok installed" > /dev/null; then
			TO_INSTALL="$TO_INSTALL""$pkg "
		fi
	done

	if [[ ! -z "$TO_INSTALL" ]]; then
		echo Need to install: $TO_INSTALL
		sudo DEBIAN_FRONTEND=noninteractive sudo apt-get -y install $PACKAGES
	fi
}

apt_install python3 python-virtualenv python3-pip \
	python3-dnspython python3-yaml python3-lxml python3-dateutil python3-pillow

# DEPLOYED TO WEB ONLY
if [ "$1" == "--deployed" ]; then
	# Get nginx from a PPA to get version 1.6 so we can support SPDY.
	if [ ! -f /etc/apt/sources.list.d/nginx-stable-trusty.list ]; then
		sudo apt_install software-properties-common # provides apt-add-repository
		sudo add-apt-repository -y ppa:nginx/stable
		sudo apt-get update
	fi

	# Install nginx, uwsgi, memcached etc.
	apt_install nginx uwsgi-plugin-python3 memcached python3-psycopg2 postgresql-client-9.3

	# Turn off nginx's default website.
	sudo rm -f /etc/nginx/sites-enabled/default

	# Put in our site.
	sudo rm -f /etc/nginx/sites-enabled/ifthenfund.conf /etc/nginx/nginx-ssl.conf
	sudo ln -s `pwd`/conf/nginx.conf /etc/nginx/sites-enabled/ifthenfund.conf
	sudo ln -s `pwd`/conf/nginx-ssl.conf /etc/nginx/nginx-ssl.conf

	# DHparams for perfect forward secrecy
	if [ ! -f /etc/ssl/local/dh2048.pem ]; then
		mkdir -p /etc/ssl/local
		sudo openssl dhparam -out /etc/ssl/local/dh2048.pem 2048
	fi

	# Fetch AWS's CA for its RDS postgres database certificates.
	# Use sslmode=verify-full and sslrootcert=/etc/ssl/certs/rds-ssl-ca-cert.pem
	sudo wget -O /etc/ssl/certs/rds-ssl-ca-cert.pem http://s3.amazonaws.com/rds-downloads/rds-combined-ca-bundle.pem

	# A place to collect static files and to serve as the virtual root.
	mkdir -p /home/ubuntu/public_html/static
	sudo service nginx restart

	# Execute pip as root because the uwsgi process starter doesn't
	# work (at least not obviously so) with a virtualenv.
	sudo easy_install3 pip # http://stackoverflow.com/questions/27341064/how-do-i-fix-importerror-cannot-import-name-incompleteread
	PIP="sudo pip3"

	# Install TLS cert provisioning tool.
	sudo apt-get install build-essential libssl-dev libffi-dev python3-dev python3-pip
	sudo pip3 install free_tls_certificates
	if [ ! -f domain_names ]; then echo "demo.if.then.fund demo.progressive.fund" > domain_names; fi

	# Install cron jobs.
	# TODO: Edit the time that cron.daily runs so that it's not when DE is running its batch processing.
	sudo rm -f /etc/cron.daily/local
	sudo ln -s `pwd`/bin/cron-daily /etc/cron.daily/local
fi

# LOCAL ONLY
if [ "$1" == "--local" ]; then
	# Create the Python virtual environment for pip package installation.
	# We use --system-site-packages to make it easier to get dependencies
	# via apt first.
	if [ ! -d .env ]; then
		virtualenv -p python3 --system-site-packages .env
	fi
	
	# Activate virtual environment.
	source .env/bin/activate

	# How shall we execute pip.
	PIP="pip -q"
fi

# Install dependencies.
$PIP install --upgrade \
	"pytz" \
	"django==1.9.7" \
	"python3-memcached" \
	"requests==2.6.0" \
	"markdown2" \
	"jsonfield" \
	"django-bootstrap3" \
	"tqdm==1.0" \
	"rtyaml" \
	"email-validator==1.0.1" \
	"git+https://github.com/JoshData/commonmark-py-plaintext" "git+https://github.com/if-then-fund/django-html-emailer" \
	"django-enumfields"

$PIP install -r \
	ext/django-email-confirm-la/requirements.txt

# LOCAL ONLY
if [ "$1" == "--local" ]; then
	# Create database / migrate database.
	./manage.py migrate

	# Create an 'admin' user.
	./manage.py createsuperuser --email=ops@if.then.fund --noinput
	# gain access with: ./manage.py changepassword ops@if.then.fund

	# Load some test data --- for testing only!
	fixtures/create-test-data

	# For testing only.
	sudo apt-get install chromium-chromedriver
	pip install selenium
fi

# DEPLOYED TO WEB ONLY
if [ "$1" == "--deployed" ]; then
	python3 manage.py collectstatic --noinput
fi
