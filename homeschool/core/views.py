import datetime

from dateutil.parser import parse
from dateutil.relativedelta import MO, SU, relativedelta
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect
from django.template.defaultfilters import pluralize
from django.urls import reverse
from django.utils import timezone
from django.views.generic import CreateView, TemplateView

from homeschool.courses.models import GradedWork
from homeschool.schools.forms import GradeLevelForm, SchoolYearForm
from homeschool.schools.models import GradeLevel, SchoolYear
from homeschool.students.models import Coursework, Grade


class IndexView(TemplateView):
    template_name = "core/index.html"


class AppView(LoginRequiredMixin, TemplateView):
    template_name = "core/app.html"

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        today = self.request.user.get_local_today()
        year = self.kwargs.get("year")
        month = self.kwargs.get("month")
        day = self.kwargs.get("day")
        if year and month and day:
            day = datetime.date(year, month, day)
        else:
            day = today
        context["day"] = day

        week = self.get_week_boundaries(day)

        # Fix the corner case when the weekly view is used and today falls in the week.
        # In that scenario, don't point at the first day of the week
        # since it messes with the UI.
        if week[0] <= today <= week[1]:
            context["day"] = today

        context["monday"], context["sunday"] = week
        context["previous_week_date"] = context["monday"] - datetime.timedelta(days=7)
        context["next_week_date"] = context["monday"] + datetime.timedelta(days=7)

        school_year = (
            SchoolYear.objects.filter(
                school=self.request.user.school,
                start_date__lte=today,
                end_date__gte=today,
            )
            .prefetch_related("grade_levels", "grade_levels__courses")
            .first()
        )

        week_dates = []
        if school_year:
            week_dates = school_year.get_week_dates_for(week)
        context["week_dates"] = week_dates

        context["schedules"] = self.get_schedules(school_year, today, week, week_dates)
        return context

    def get_week_boundaries(self, today):
        """Get the Monday and Sunday that bound today."""
        monday = today + relativedelta(weekday=MO(-1))
        sunday = today + relativedelta(weekday=SU(+1))
        return monday, sunday

    def get_schedules(self, school_year, today, week, week_dates):
        """Get the schedules for each student."""
        schedules = []
        if school_year is None:
            return schedules

        for student in self.request.user.school.students.all():
            courses = student.get_courses(school_year)
            week_coursework = student.get_week_coursework(week)
            schedule = self.get_student_schedule(
                student, today, week, week_dates, courses, week_coursework
            )
            schedules.append(schedule)

        return schedules

    def get_student_schedule(
        self, student, today, week, week_dates, courses, week_coursework
    ):
        """Get the schedule.

        Each student will get a list of courses, filled with each day.
        Empty slots will contain None.
        """
        week_start_date = week[0]
        week_end_date = week[1]
        completed_task_ids = list(
            Coursework.objects.filter(
                student=student, course_task__course__in=courses
            ).values_list("course_task_id", flat=True)
        )
        task_limit = len(week_dates)
        schedule = {"student": student, "courses": []}
        for course in courses:
            course_schedule = {"course": course, "days": []}

            course_tasks = []
            # Only show tasks on current or future weeks.
            if week_end_date >= today:
                task_index = 0
                if week_start_date > today:
                    task_index = self.get_future_course_task_index(
                        student, course, today, week_start_date
                    )

                # Doing this query in a loop is definitely an N+1 bug.
                # If it's possible to do a single query of all tasks
                # that groups by course then that would be better.
                # No need to over-optimize until that's a real issue.
                # I brought this up on the forum. It doesn't look like it's easy to fix.
                # https://forum.djangoproject.com/t/grouping-by-foreignkey-with-a-limit-per-group/979
                course_tasks = list(
                    course.course_tasks.exclude(id__in=completed_task_ids)[
                        task_index : task_index + task_limit
                    ]
                )
                course_tasks.reverse()  # for the performance of pop below.

            for week_date in week_dates:
                course_schedule_item = {"week_date": week_date}
                if (
                    course.id in week_coursework
                    and week_date in week_coursework[course.id]
                ):
                    coursework_list = week_coursework[course.id][week_date]
                    course_schedule_item["coursework"] = coursework_list
                elif course.runs_on(week_date) and course_tasks:
                    course_schedule_item["task"] = course_tasks.pop()
                course_schedule["days"].append(course_schedule_item)
            schedule["courses"].append(course_schedule)
        return schedule

    def get_future_course_task_index(self, student, course, today, week_start_date):
        """Get the db index of course tasks for future weeks.

        This is based on the last completed coursework for the course.
        """
        this_week = self.get_week_boundaries(today)
        latest_coursework = (
            Coursework.objects.filter(student=student, course_task__course=course)
            .order_by("-completed_date")
            .first()
        )
        if latest_coursework and (this_week[0] <= latest_coursework.completed_date):
            start_date = latest_coursework.completed_date + datetime.timedelta(days=1)
        else:
            # When the student has no coursework yet, the counting should start
            # from the week start date relative to today.
            start_date = this_week[0]
        return course.get_task_count_in_range(
            start_date, week_start_date - datetime.timedelta(days=1)
        )


class DailyView(LoginRequiredMixin, TemplateView):
    template_name = "core/daily.html"

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        today = self.request.user.get_local_today()
        year = self.kwargs.get("year")
        month = self.kwargs.get("month")
        day = self.kwargs.get("day")
        if year and month and day:
            day = datetime.date(year, month, day)
        else:
            day = today
        context["day"] = day

        school_year = (
            SchoolYear.objects.filter(
                school=self.request.user.school, start_date__lte=day, end_date__gte=day
            )
            .prefetch_related("grade_levels", "grade_levels__courses")
            .first()
        )

        # Set previous and next days navigation.
        if school_year:
            context["yesterday"] = school_year.get_previous_day_from(day)
            context["ereyesterday"] = school_year.get_previous_day_from(
                context["yesterday"]
            )
            context["tomorrow"] = school_year.get_next_day_from(day)
            context["overmorrow"] = school_year.get_next_day_from(context["tomorrow"])
        else:
            context["ereyesterday"] = day - datetime.timedelta(days=2)
            context["yesterday"] = day - datetime.timedelta(days=1)
            context["tomorrow"] = day + datetime.timedelta(days=1)
            context["overmorrow"] = day + datetime.timedelta(days=2)

        context["schedules"] = self.get_schedules(school_year, today, day)
        return context

    def get_schedules(self, school_year, today, day):
        """Get the schedules for each student."""
        schedules = []
        if not school_year:
            return schedules

        if not school_year.runs_on(day):
            return schedules

        for student in self.request.user.school.students.all():
            courses = student.get_courses(school_year)
            schedule = self.get_student_schedule(student, today, day, courses)
            schedules.append(schedule)

        return schedules

    def get_student_schedule(self, student, today, day, courses):
        """Get the daily schedule for the student."""
        day_coursework = student.get_day_coursework(day)
        completed_task_ids = list(
            Coursework.objects.filter(
                student=student, course_task__course__in=courses
            ).values_list("course_task_id", flat=True)
        )
        schedule = {"student": student, "courses": []}
        for course in courses:
            course_schedule = {"course": course}
            if course.id in day_coursework:
                course_schedule["coursework"] = day_coursework[course.id]
            elif course.runs_on(day):
                task_index = max(course.get_task_count_in_range(today, day) - 1, 0)
                # Doing this query in a loop is definitely an N+1 bug.
                # If it's possible to do a single query of all tasks
                # that groups by course then that would be better.
                # No need to over-optimize until that's a real issue.
                # I brought this up on the forum. It doesn't look like it's easy to fix.
                # https://forum.djangoproject.com/t/grouping-by-foreignkey-with-a-limit-per-group/979
                try:
                    course_task = course.course_tasks.exclude(
                        id__in=completed_task_ids
                    )[task_index]
                except IndexError:
                    course_task = None
                course_schedule["task"] = course_task
            schedule["courses"].append(course_schedule)
        return schedule

    def post(self, request, *args, **kwargs):
        """Process students' work."""
        completed_date = timezone.now().date()
        if "completed_date" in request.POST:
            completed_date = parse(request.POST["completed_date"])

        tasks_by_student = self.get_task_completions_by_student(request.POST)
        work_to_grade = False
        if tasks_by_student:
            for student_id, tasks in tasks_by_student.items():
                student = request.user.school.students.filter(id=student_id).first()
                has_work_to_grade = self.mark_completion(student, tasks, completed_date)
                if has_work_to_grade:
                    work_to_grade = True

        if work_to_grade:
            success_url = self.get_grade_url()
        else:
            success_url = request.GET.get("next", reverse("core:daily"))
        return HttpResponseRedirect(success_url)

    def get_task_completions_by_student(self, post_data):
        """Parse out the tasks."""
        tasks = {}
        for key, value in post_data.items():
            if not key.startswith("task"):
                continue
            parts = key.split("-")
            student_id = int(parts[1])
            task_id = int(parts[2])

            if student_id not in tasks:
                tasks[student_id] = {"complete": [], "incomplete": []}

            category = "complete" if value == "on" else "incomplete"
            tasks[student_id][category].append(task_id)
        return tasks

    def get_grade_url(self):
        grade_url = reverse("students:grade")
        next_url = self.request.GET.get("next", reverse("core:daily"))
        return f"{grade_url}?next={next_url}"

    def mark_completion(self, student, tasks, completed_date):
        """Mark completed tasks or clear already complete tasks."""
        if not student:
            return

        has_work_to_grade = self.process_complete_tasks(
            student, tasks["complete"], completed_date
        )
        self.process_incomplete_tasks(student, tasks["incomplete"])
        return has_work_to_grade

    def process_complete_tasks(self, student, complete_task_ids, completed_date):
        """Add coursework for any tasks that do not have it."""
        has_work_to_grade = False
        existing_complete_task_ids = set(
            Coursework.objects.filter(
                student=student, course_task__in=complete_task_ids
            ).values_list("course_task_id", flat=True)
        )
        newly_complete_task_ids = set(complete_task_ids) - existing_complete_task_ids
        if newly_complete_task_ids:
            new_coursework = []
            for task_id in newly_complete_task_ids:
                new_coursework.append(
                    Coursework(
                        student=student,
                        course_task_id=task_id,
                        completed_date=completed_date,
                    )
                )
            Coursework.objects.bulk_create(new_coursework)

            pluralized = pluralize(len(newly_complete_task_ids))
            message = "Completed {} task{} for {}.".format(
                len(newly_complete_task_ids), pluralized, student.full_name
            )
            messages.add_message(self.request, messages.SUCCESS, message)

            graded_work_ids = set(
                GradedWork.objects.filter(
                    course_task__in=newly_complete_task_ids
                ).values_list("id", flat=True)
            )
            already_graded_work_ids = set(
                Grade.objects.filter(
                    student=student, graded_work__in=graded_work_ids
                ).values_list("graded_work_id", flat=True)
            )
            has_work_to_grade = bool(graded_work_ids - already_graded_work_ids)

        return has_work_to_grade

    def process_incomplete_tasks(self, student, incomplete_task_ids):
        """Remove any coursework for tasks that are marked as incomplete."""
        delete_info = Coursework.objects.filter(
            student=student, course_task__in=incomplete_task_ids
        ).delete()
        coursework_deleted = delete_info[0]
        if coursework_deleted > 0:
            pluralized = pluralize(coursework_deleted)
            message = "Undid {} task{} for {}.".format(
                coursework_deleted, pluralized, student.full_name
            )
            messages.add_message(self.request, messages.SUCCESS, message)


class StartView(LoginRequiredMixin, TemplateView):
    template_name = "core/start.html"


class StartSchoolYearView(LoginRequiredMixin, CreateView):
    template_name = "core/start_school_year.html"
    form_class = SchoolYearForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["school_year"] = SchoolYear.objects.filter(
            school=self.request.user.school
        ).first()
        return context

    def get_success_url(self):
        return reverse("core:start-grade-level")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs


class StartGradeLevelView(LoginRequiredMixin, CreateView):
    template_name = "core/start_grade_level.html"
    form_class = GradeLevelForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["grade_level"] = GradeLevel.objects.filter(
            school_year__school=self.request.user.school
        ).first()
        context["school_year"] = SchoolYear.objects.filter(
            school=self.request.user.school
        ).first()
        return context

    def get_success_url(self):
        return reverse("core:start-course")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs


class StartCourseView(LoginRequiredMixin, TemplateView):
    template_name = "core/start_course.html"


class StartCourseTaskView(LoginRequiredMixin, TemplateView):
    template_name = "core/start_course.html"
