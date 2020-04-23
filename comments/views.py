from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from auth.helpers import auth_required
from club.exceptions import AccessDenied, RateLimitException
from comments.forms import CommentForm
from comments.models import Comment, CommentVote
from common.request import parse_ip_address, parse_useragent, ajax_request
from posts.models import Post, PostView


@auth_required
def create_comment(request, post_slug):
    post = get_object_or_404(Post, slug=post_slug)
    if not post.is_commentable and not request.me.is_moderator:
        raise AccessDenied(title="Комментарии к этому посту закрыты")

    if request.method == "POST":
        form = CommentForm(request.POST)
        if form.is_valid():
            is_ok = Comment.check_rate_limits(request.me)
            if not is_ok:
                raise RateLimitException(
                    title="🙅‍♂️ Вы комментируете слишком часто",
                    message="Подождите немного, вы достигли нашего лимита на комментарии в день. "
                            "Можете написать нам в саппорт, пожаловаться об этом."
                )

            comment = form.save(commit=False)
            comment.post = post
            if not comment.author:
                comment.author = request.me

            comment.ipaddress = parse_ip_address(request)
            comment.useragent = parse_useragent(request)

            if form.cleaned_data["reply_to_id"]:
                # stupid django can't do that from forms
                comment.reply_to_id = form.cleaned_data["reply_to_id"]

            comment.save()

            # update the shitload of counters :)
            request.me.update_last_activity()
            Comment.update_post_counters(post)
            PostView.increment_unread_comments(post)
            PostView.create_or_update(
                request=request,
                user=request.me,
                post=post,
            )

            return redirect("show_comment", post.slug, comment.id)
        else:
            return render(request, "error.html", {
                "title": "Какая-то ошибка при публикации комментария 🤷‍♂️",
                "message": f"Не знаем что происходит, но мы уже фиксим. "
                           f"Мы сохранили ваш коммент чтобы вы не потеряли его:",
                "data": form.cleaned_data.get("text")
            })

    return Http404()


def show_comment(request, post_slug, comment_id):
    post = get_object_or_404(Post, slug=post_slug)
    return redirect(
        reverse("show_post", kwargs={"post_type": post.type, "post_slug": post.slug}) + f"#comment-{comment_id}"
    )


@auth_required
def edit_comment(request, comment_id):
    comment = get_object_or_404(Comment, id=comment_id)

    if not request.me.is_moderator:
        if comment.author != request.me:
            raise AccessDenied()

        if not comment.is_editable:
            raise AccessDenied(title="Этот комментарий больше нельзя редактировать")

        if not comment.post.is_visible or not comment.post.is_commentable:
            raise AccessDenied(title="Комментарии к этому посту были закрыты")

    post = comment.post

    if request.method == "POST":
        form = CommentForm(request.POST, instance=comment)
        if form.is_valid():
            comment = form.save(commit=False)
            comment.is_deleted = False
            comment.html = None  # flush cache
            comment.ipaddress = parse_ip_address(request)
            comment.useragent = parse_useragent(request)
            comment.save()
            return redirect("show_comment", post.slug, comment.id)
    else:
        form = CommentForm(instance=comment)

    return render(request, "comments/edit.html", {
        "comment": comment,
        "post": post,
        "form": form
    })


@auth_required
def delete_comment(request, comment_id):
    comment = get_object_or_404(Comment, id=comment_id)

    if not request.me.is_moderator:
        # only comment author, post author or moderator can delete comments
        if comment.author != request.me and request.me != comment.post.author:
            raise AccessDenied(
                title="Нельзя!",
                message="Только автор комментария, поста или модератор может удалить комментарий"
            )

        if not comment.is_editable:
            raise AccessDenied(
                title="Время вышло",
                message="Комментарий можно отредактировать или удалить только в первые несколько часов их жизни"
            )

        if not comment.post.is_visible:
            raise AccessDenied(
                title="Пост скрыт!",
                message="Нельзя удалять комментарии к скрытому посту"
            )

    if not comment.is_deleted:
        # delete comment
        comment.delete(deleted_by=request.me)
    else:
        # undelete comment
        if comment.deleted_by == request.me or request.me.is_moderator:
            comment.undelete()
        else:
            raise AccessDenied(
                title="Нельзя!",
                message="Только тот, кто удалил комментарий, может его восстановить"
            )

    Comment.update_post_counters(comment.post)

    return redirect("show_comment", comment.post.slug, comment.id)


@auth_required
def pin_comment(request, comment_id):
    comment = get_object_or_404(Comment, id=comment_id)

    if not request.me.is_moderator and comment.post.author != request.me:
        raise AccessDenied(
            title="Нельзя!",
            message="Только автор поста или модератор может пинить посты"
        )

    comment.is_pinned = not comment.is_pinned  # toggle
    comment.save()

    return redirect("show_comment", comment.post.slug, comment.id)


@auth_required
@ajax_request
def upvote_comment(request, comment_id):
    if request.method != "POST":
        raise Http404()

    comment = get_object_or_404(Comment, id=comment_id)

    _, is_created = CommentVote.upvote(
        request=request,
        user=request.me,
        comment=comment,
    )

    return {
        "comment": {
            "upvotes": comment.upvotes + (1 if is_created else 0)
        }
    }