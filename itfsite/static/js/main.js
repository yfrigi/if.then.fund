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
			// on the home and login page, dont redirect back to those pages after a login but instead go to the users account page
			$('.log-inout-link a').each(function() {
					$(this).attr('href', $(this).attr('href') + '?next=' + encodeURIComponent(window.location.pathname));
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
	$('#page-sections-nav').attr('data-offset-top', $('header').height());
	$('#page-sections-nav').attr('data-offset-bottom', $('#page-sections-nav').height());

	// enable scrollspy
	$('body')
		.css({ 'position': 'relative' })
		.scrollspy({ target: '#page-sections-nav', offset: 70 });
}
