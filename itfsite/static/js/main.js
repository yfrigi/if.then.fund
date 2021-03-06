function show_django_messages() {
	// Django Messages
	if (typeof django_messages != "undefined" && django_messages.length > 0) {
		var msg = $("<div></div>");
		for (var i = 0; i < django_messages.length; i++) {
			var n = $("<p/>")
				.addClass(django_messages[i].level_tag)
				.text(django_messages[i].message);
			msg.append(n)
		}
		show_modal_error('', msg)
	}
}

function set_login_state() {
	// User Login Buttons
	if (typeof the_user != "undefined") {
		if (!the_user)
			$('.if-anonymous').removeClass('hidden')
		else
			$('.if-logged-in').removeClass('hidden')
		if (window.location.pathname != '/' && window.location.pathname != '/accounts/login') {
			// Set the "next" parameter in the URLs on the log in & log out buttons. Form
			// a relative URL using location.pathname + location.search.
			// On the home and login page, dont redirect back to those pages after a login but
			// instead go to the users account page.
			$('.log-inout-link a').each(function() {
					$(this).attr('href',
						   $(this).attr('href')
						 + '?next='
						 + encodeURIComponent(window.location.pathname + window.location.search));
				});
		}

		$('.notifications-link a').click(show_notifications);
		if (the_user && the_user.notifications) {
			the_user.notifications.forEach(function(item) {
				if (item.new)
					$('.notifications-link').addClass('has-new-notifications');
			});
		}
	}
}

function build_page_sections_nav() {
	// Page Affixed Nav: Create the nav items dynamically from the h2's.
	if ($('#page-sections-nav').length == 0)
		return;
	
	// Create nav elements.
	$('h2').each(function() {
		var target = $(this);
		var text = target.text();
		if (target.attr('data-nav-text')) text = target.attr('data-nav-text');
		var id = text.replace(/\W/g, '').toLowerCase();
		var n = $("<li role=\"presentation\"><a></a></li>");
		n.find('a').attr('href', '#' + id);
		n.find('a').text(text);
		function my_click_handler() {
			smooth_scroll_to(target);
			if (history.pushState)
				history.pushState({}, "", "#" + id);
			return false;
		} 
		n.find('a').click(my_click_handler);
		target[0].setAttribute('id', id);
		$('#page-sections-nav ul').append(n);

		// for the xs non-affixed, non-scrollspy'd version
		var n2 = n.clone();
		n2.find('a').click(my_click_handler);
		$('#page-sections-nav-xs ul').append(n2);
	});

	// Set required attributes.
	function set_width() { $('#page-sections-nav').css('width', $('#page-sections-nav').parent().width()); }
	set_width();
	$(window).resize(set_width);
	$('#page-sections-nav').attr('data-offset-top', $('header').height());
	$('#page-sections-nav').attr('data-offset-bottom', $('#page-sections-nav').height());

	// enable scrollspy
	$('body')
		.css({ 'position': 'relative' })
		.scrollspy({ target: '#page-sections-nav', offset: 70 });
}

function make_fixed_header() {
	if ($('#page-fixed-header').length == 0)
		return;
	
	var h1top = $('h1').offset().top;
	var h1bot = $('h1').offset().top + $('h1').outerHeight();

	$(window).on('scroll', function() {
		var top = $(window).scrollTop();
		if (top <= h1top/2)
			$('#page-fixed-header').stop().hide();
		else if (top < h1bot - 30)
			$('#page-fixed-header').stop().fadeOut();
		else
			$('#page-fixed-header').stop().fadeIn();
	})

	$('#page-fixed-header').click(function() {
		smooth_scroll_to($('body'))
	})
}

function set_css_to_maximum(elems, property) {
	// elems should be a jQuery object.
	// property should be 'width' or 'height'.
	// Gets the maximum outerWidth/Height of the elements (because bootstrap sets the box model
	// to use outer dimensions?) and then sets that as the CSS width/height of all the elements.
	var max_value = 0;
	elems.css(property, "auto"); // in case a width/height has been set by a previous call, undo it so it recalculates dimensions
	elems.each(function() {
		var v = $(this)[property == 'width' ? 'outerWidth' : 'outerHeight'](); // e.g. elem.outerWidth() or elem.outerHeight()
		if (v > max_value) max_value = v;
	});
	elems.css(property, max_value + "px");
}

function show_notifications() {
	// Create the element that contains the notifications popup.
	var node = $('<div id="notifications-popup"> </div>');

	// Build the contents of the popup.
	function push_notification(alert) {
		var n = $('<div class="notification-alert"></div>');
		if (alert.new) n.addClass('new');
		n.append($("<div>" + alert.body_html + "</div>"));
		var d = $("<div class='date'/>")
		n.append(d);
		if (alert.date) d.text(moment(alert.date).calendar());

		node.append(n)
	}
	node.text('');
	if (!the_user || !the_user.notifications || !the_user.notifications.length) {
		push_notification({ body_html: "No notifications."})
	} else {
		var ids = Array();
		for (var i = 0; i < the_user.notifications.length; i++) {
			var n = the_user.notifications[i];
			push_notification(n)
			ids = ids.concat(n.ids)
		}

		// Mark all of the notifications shown as dismissed.
		$.ajax({
			url: '/accounts/_dismiss-notifications',
			method: 'POST',
			data: {
				ids: ids.join(',')
			}
		})
		$('.notifications-link').removeClass('has-new-notifications');
	}

	// Display it.
	node.find(">div").width(Math.max(300, $(window).width()/3))
	$('.navbar .notifications-link').popover({
		placement: 'bottom',
		title: 'Notifications',
		container: 'body',
		html: true,
		content: function() {
			return "<div class='notifications-popup'>" + $(node).html() + "</div>";
		}	
	})
}
