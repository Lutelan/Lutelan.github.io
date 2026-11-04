---
layout: default
title: Home
---

# ~/lutelan

Hey, I'm Lutelan. Welcome to my little corner of the internet.

I write about cybersecurity, CTFs, Linux, programming, and whatever else I find interesting.

[About Me](/about/) | [GitHub](https://github.com/Lutelan)

---

## ~/blog/posts

{% for post in site.posts %}

### [{{ post.title }}]({{ post.url | relative_url }})

*{{ post.date | date: "%B %d, %Y" }}*

{{ post.description | default: post.excerpt | strip_html | truncatewords: 35 }}

[Read more →]({{ post.url | relative_url }})

---

{% else %}

No posts yet.

{% endfor %}
