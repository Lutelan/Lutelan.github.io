---
layout: default
title: Home
---

# About Me

Hello everyone, I'm Lutelan, this site will serve as a blog and as a knowledge base for things I do from time to time. CTF problems I solve, cool things I code etc. 
Hope you have fun reading the posts and learn something new!

[GitHub](https://github.com/Lutelan)

---

## Posts

{% for post in site.posts %}

### [{{ post.title }}]({{ post.url | relative_url }})

*{{ post.date | date: "%B %d, %Y" }}*

{{ post.description | default: post.excerpt | strip_html | truncatewords: 35 }}

[Read more →]({{ post.url | relative_url }})

---

{% else %}

No posts yet.

{% endfor %}
